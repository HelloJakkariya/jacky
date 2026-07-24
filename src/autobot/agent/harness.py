"""The one agent loop, shared by every provider.

Extracted verbatim (behavior-preserving) from the duplicated ``run_turn`` loops
that used to live in ``llm/ollama_llm.py`` and ``llm/anthropic_llm.py``. The loop
drives a :class:`~autobot.agent.chat_model.ChatModel`: send → dispatch tool calls
through the injected executor (the permission gate) → feed results back → repeat,
until the model returns no tool calls (the final answer), a round only re-issues
already-failed calls, an identical call repeats past the doom-loop guard, or the
round cap is hit (then one tools-disabled call forces a final answer).

The harness owns the conversation :class:`~autobot.agent.session.Session` (not the
model): it creates the initial session, threads it through every primitive call,
and persists each turn's new messages via the injected
:class:`~autobot.agent.session_store.SessionStore` once the turn finalizes. This is
what keeps the provider adapters stateless across turns.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autobot.core.streaming import active_session_id, current_executor, output_sink
from autobot.core.types import ErrorCategory, ToolCall, ToolResult
from autobot.logging_setup import get_logger
from autobot.mcp.adapter import split_namespaced

if TYPE_CHECKING:
    from autobot.agent.chat_model import ChatModel, OnEvent
    from autobot.agent.session import Session
    from autobot.agent.session_store import SessionStore
    from autobot.core.types import ToolExecutor
    from autobot.tasks import NotificationInbox

_log = get_logger("harness")

_MAX_TOOL_ROUNDS = 8  # runaway backstop on the plan→tool→result loop (insurance, not the
# normal stop — natural completion + progress guards below end a turn first)
_DOOM_LIMIT = 4  # abort if one identical (name+args) call repeats this many times
_MAX_UNPRODUCTIVE_ROUNDS = 3  # stop after this many rounds in a row with NO successful tool
# Phase 2 TODO: when polling/wait tools arrive, key this on CONSECUTIVE identical
# calls with no intervening progress (not a per-turn total) so legitimate polling
# with identical args isn't cut off.


def _call_key(call: ToolCall) -> str:
    """Stable identity for a call (name + canonical args) for anti-thrash/doom checks."""
    return call.name + "\0" + json.dumps(call.arguments, sort_keys=True, default=str)


def _file_name(raw: str) -> str:
    """Just the file name for display (``/a/b/app.py`` or ``src/app.py`` → ``app.py``).

    The activity line is a quick at-a-glance indicator, so the bare name reads easier than a
    long path; falls back to the raw string when there's no name component. Pure — no fs I/O.
    """
    if not raw:
        return raw
    try:
        return Path(raw).name or raw
    except (OSError, ValueError):
        return raw


def tool_label(call: ToolCall) -> str:
    """A short human label for a tool call, for the activity line (paths shown as the file name)."""
    args = call.arguments
    if call.name == "read_file":
        return f"Read {_file_name(str(args.get('path', '')))}".strip()
    if call.name == "grep":
        return f'Searched "{args.get("pattern", "")}"'
    if call.name in ("glob", "list_dir"):
        target = args.get("pattern") or _file_name(str(args.get("path", "")))
        return f"Listed {target}".strip()
    if call.name == "run_command":
        return f"$ {str(args.get('command', ''))[:80]}"
    if call.name == "read_files":
        paths = args.get("paths") or []
        return f"Read {len(paths)} file(s)" if isinstance(paths, list) else "Read files"
    if call.name in ("write_file", "edit_file", "multi_edit"):
        return f"Edited {_file_name(str(args.get('path', '')))}".strip()
    if call.name == "multi_patch":
        files = args.get("files") or []
        return f"Edited {len(files)} file(s)" if isinstance(files, list) else "Edited files"
    if call.name == "delete_file":
        return f"Deleted {_file_name(str(args.get('path', '')))}".strip()
    if call.name == "move_file":
        return f"Moved {_file_name(str(args.get('source', '')))}".strip()
    if call.name == "symbol":
        return f"Looked up {str(args.get('name', ''))[:40]}".strip()
    if call.name == "rename_symbol":
        return f"Renamed {str(args.get('name', ''))[:24]} → {str(args.get('new_name', ''))[:24]}"
    if call.name == "diagnostics":
        return f"Checked {_file_name(str(args.get('path', '')))}".strip()
    if call.name == "spawn_agent":
        return f"Spawned subagent: {str(args.get('label') or args.get('task', ''))[:70]}".strip()
    if call.name == "skill":
        return f"Skill: {str(args.get('name', ''))[:40]}".strip()
    if call.name == "read_skill_file":
        name, path = str(args.get("name", "")), str(args.get("path", ""))
        return f"Skill file {name}/{path}".strip("/ ") or "Skill file"
    # MCP tools (<server>__<tool>): show the server as the label's subject so the activity
    # line reads "github: create issue" instead of a mangled capitalization.
    ns = split_namespaced(call.name)
    if ns is not None:
        server, bare = ns
        return f"{server}: {bare.replace('_', ' ')}".strip()
    # Fallback for any other tool: humanize the raw name so casing stays consistent with the
    # verb-prefixed labels above (e.g. "repo_map" → "Repo map", "update_plan" → "Update plan").
    return call.name.replace("_", " ").strip().capitalize()


class AgentHarness:
    """Runs one user turn end-to-end against a :class:`ChatModel`, owning the Session."""

    def __init__(
        self,
        model: ChatModel,
        store: SessionStore,
        *,
        cwd: str = ".",
        model_name: str = "",
        max_rounds: int = _MAX_TOOL_ROUNDS,
        doom_limit: int = _DOOM_LIMIT,
        max_unproductive: int = _MAX_UNPRODUCTIVE_ROUNDS,
        redact: Callable[[str], str] | None = None,
        inbox: NotificationInbox | None = None,
    ) -> None:
        """Wire the harness.

        Args:
            model: The provider adapter driving one turn.
            store: Persists the conversation session across turns.
            cwd: Working directory recorded on the session.
            model_name: Model identifier recorded on the session.
            max_rounds: Runaway backstop on the plan-tool-result loop per turn.
            doom_limit: Times an identical call may repeat before aborting.
            max_unproductive: Stop the turn after this many consecutive rounds that ran
                tools but produced no successful result (diminishing-returns guard) — so a
                model flailing with new-but-failing calls stops well before ``max_rounds``.
            redact: Optional scrubber applied to each tool result's content right
                before it is handed to the model (e.g. to strip secrets from tool
                output before any provider sees it). ``None`` (the default) passes
                content through unchanged.
            inbox: Optional per-session notification inbox. When set, any completion
                notes for this session (e.g. a backgrounded command that finished) are
                folded into the next turn's context so the model acts on them without the
                user re-prompting. ``None`` disables delivery.
        """
        self._model = model
        self._store = store
        self._cwd = cwd
        self._model_name = model_name
        self._max_rounds = max_rounds
        self._doom_limit = doom_limit
        self._max_unproductive = max_unproductive
        self._redact = redact
        self._inbox = inbox
        self._session = store.create(cwd, model_name)

    @property
    def session(self) -> Session:
        """The current conversation session."""
        return self._session

    def run_turn(
        self,
        user_text: str,
        execute: ToolExecutor,
        on_event: OnEvent | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> str:
        """Handle one user turn end-to-end; tool calls run through ``execute`` (the gate).

        When ``on_event`` is provided, emits ``{"type": "tool", "event": "start"/"end", ...}``
        around each executed tool call (and token events come from the provider's ``send``).

        When ``should_cancel`` is provided, it is polled at the top of every tool round; the
        first time it returns ``True`` the turn stops cooperatively (the current round is not
        started) and returns a short interrupted reply. The turn is still finalized, so the
        partial conversation is persisted. Cancellation is between rounds — an in-flight
        provider ``send`` or tool call is not force-killed (see ``esc``/``/coder/interrupt``).
        """

        def emit(evt: dict[str, Any]) -> None:
            if on_event is None:
                return
            try:
                on_event(evt)
            except Exception:  # a bad sink must never break a turn
                _log.exception("on_event sink failed; continuing turn")

        # Expose this session's id for the turn so a backgrounded run_command can tag its
        # task and route its completion note back here. Re-set at the top of every turn, so a
        # leak on an error path is self-correcting; reset on the normal path below.
        sid_token = active_session_id.set(self._session.id)
        # Expose the gate-wired executor so a meta-tool (e.g. run_workflow) can run its
        # steps through the same gate. Same self-correcting set/reset pattern as above.
        exec_token = current_executor.set(execute)
        user_text = self._fold_notifications(user_text)
        self._model.begin_turn(self._session, user_text)
        failed: dict[str, str] = {}  # anti-thrash: call key -> failure text
        seen: dict[str, int] = {}  # doom-loop: call key -> times issued this turn
        reply = ""
        unproductive = 0  # consecutive rounds that ran tools but produced no successful result
        for _ in range(self._max_rounds):
            if should_cancel is not None and should_cancel():
                _log.info("turn interrupted (cancel requested)")
                reply = "Interrupted."
                break
            resp = self._model.send(self._session, on_event)
            if not resp.tool_calls:
                reply = resp.text
                break
            _log.info("planned tools=%s", [c.name for c in resp.tool_calls])
            results: list[tuple[ToolCall, ToolResult]] = []
            all_repeat = True  # did this round only re-issue calls that already failed?
            progressed = False  # did any tool this round actually succeed (real forward progress)?
            last_fail = ""
            doomed = False
            # Fail-fast within a batch: the model can't have seen an earlier call's failure yet
            # (results feed back only after the whole batch, at record_results below), so a later
            # call that depends on it would run against bad/half-done state. Once one call fails,
            # stop executing the rest of THIS batch and defer them to the next round with an
            # explicit result — the model reads the real error, then re-plans.
            batch_failed = False
            blocker_label = ""
            deferred = 0
            for call in resp.tool_calls:
                if batch_failed:
                    # Deferred, not failed: intentionally NOT recorded in `seen`/`failed`, so the
                    # model may re-issue this call freely once the blocker is resolved.
                    deferred += 1
                    results.append(
                        (
                            call,
                            ToolResult(
                                name=call.name,
                                content=(
                                    f"skipped — not run because an earlier step in this batch "
                                    f"({blocker_label}) failed. Read that error, then re-issue "
                                    "this call if it's still needed."
                                ),
                                ok=False,
                                category=ErrorCategory.DEPENDENCY,
                            ),
                        )
                    )
                    continue
                discovery = self._model.handle_discovery(self._session, call)
                if discovery is not None:
                    all_repeat = False  # discovery is real progress, not a failing repeat
                    progressed = True
                    results.append((call, ToolResult(name=call.name, content=discovery, ok=True)))
                    continue
                key = _call_key(call)
                seen[key] = seen.get(key, 0) + 1
                if seen[key] >= self._doom_limit:
                    doomed = True
                if key in failed:
                    out, ok = failed[key], False  # already failed — reuse, don't re-run
                    last_fail = out
                    # NOT a fail-fast trigger: a reused failure was already surfaced to the
                    # model in an earlier round, so this batch was planned with it known — let
                    # any independent sibling calls run (the all_repeat guard handles thrash).
                else:
                    all_repeat = False
                    emit(
                        {
                            "type": "tool",
                            "event": "start",
                            "name": call.name,
                            "label": tool_label(call),
                        }
                    )

                    # Expose a live output sink for the duration of this tool call so a
                    # streaming tool (run_command) can surface each line to the CLI as it
                    # runs; reset after so nothing leaks between calls.
                    def _emit_output(line: str, _name: str = call.name) -> None:
                        emit({"type": "output", "text": line, "name": _name})

                    sink_token = output_sink.set(_emit_output)
                    try:
                        result = execute(call)  # through the permission gate
                    finally:
                        output_sink.reset(sink_token)
                    out, ok = result.content, result.ok
                    emit(
                        {
                            "type": "tool",
                            "event": "end",
                            "name": call.name,
                            "label": tool_label(call),
                            "ok": ok,
                        }
                    )
                    if result.ok:
                        progressed = True
                    else:
                        failed[key] = out
                        last_fail = out
                        batch_failed = True  # short-circuit any dependent follow-ups in this batch
                        blocker_label = tool_label(call)
                results.append((call, ToolResult(name=call.name, content=out, ok=ok)))
            if deferred:
                _log.info(
                    "batch fail-fast: %s failed; deferred %d follow-up call(s) to next round",
                    blocker_label,
                    deferred,
                )
            if self._redact is not None:
                # Egress chokepoint: scrub secret-shaped tool output before it enters the
                # conversation the model sees, regardless of provider.
                results = [
                    (call, replace(result, content=self._redact(result.content)))
                    for call, result in results
                ]
            self._model.record_results(self._session, results)
            if doomed:
                _log.info("stopping: identical tool call repeated past doom-loop guard")
                reply = "I kept trying the same step without progress, so I stopped."
                break
            if all_repeat:  # model is just retrying a failing step — stop and explain
                _log.info("stopping: round repeated only previously-failed tool calls")
                reply = last_fail or "I couldn't complete that, so I stopped."
                break
            unproductive = 0 if progressed else unproductive + 1
            if unproductive >= self._max_unproductive:
                _log.info("stopping: %d rounds with no successful progress", unproductive)
                reply = last_fail or "I couldn't make progress after several tries, so I stopped."
                break
        else:
            reply = self._model.final_answer_no_tools(self._session)
        new_events = self._model.finalize_turn(self._session)
        self._store.append(self._session, new_events)
        active_session_id.reset(sid_token)
        current_executor.reset(exec_token)
        return reply

    def _fold_notifications(self, user_text: str) -> str:
        """Prepend any pending completion notes for this session to ``user_text``.

        Delivers the results of tasks that finished off the turn (e.g. a backgrounded
        command) so the model sees them at the start of its next turn without the user
        re-prompting. A no-op when no inbox is wired or nothing is pending.
        """
        if self._inbox is None:
            return user_text
        notes = self._inbox.drain(self._session.id)
        if not notes:
            return user_text
        _log.info("delivering %d background notification(s)", len(notes))
        header = "Results from background tasks that finished (act on any that matter):"
        body = "\n".join(f"- {note}" for note in notes)
        return f"{header}\n{body}\n\n{user_text}"

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot completion — delegated to the model (no tools, no loop)."""
        return self._model.complete(prompt, temperature=temperature)

    def context_usage(self) -> dict[str, Any] | None:
        """The current session's context-meter payload."""
        return self._session.last_usage

    def session_id(self) -> str:
        """The current session's id (for filtering the usage ledger to this session)."""
        return self._session.id

    def new_session(self) -> None:
        """Discard the current conversation and start a fresh session."""
        self._session = self._store.create(self._session.cwd, self._session.model)

    def set_delivery_mode(self, mode: str) -> None:
        """Set how the next reply is delivered ('chat' = text, else spoken)."""
        self._session.delivery_mode = mode

    def resume(self, session_id: str) -> bool:
        """Replace the current session with a stored one, or leave it unchanged.

        Returns:
            ``True`` if ``session_id`` was found and loaded, ``False`` otherwise.
        """
        loaded = self._store.load(session_id)
        if loaded is None:
            return False
        self._session = loaded
        return True

    def list_sessions(self) -> list[dict[str, Any]]:
        """Summaries of all stored sessions (id/cwd/model/mtime), most recent first."""
        return self._store.list()
