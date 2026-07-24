"""In-session slash commands: parse a ``/name args`` line and dispatch it.

Commands are pure — they return a :class:`CommandResult` the UI shell enacts (print a
message, clear the transcript, or exit) — so they're testable without a UI and reused by
any shell. This module only owns parsing plus the client-side trio (``/help``, ``/clear``,
``/exit``); the daemon-backed commands (``/diff``, ``/undo``, ``/model``, ``/autonomy``,
``/sessions``, ``/new``) are handled by the ``coder_commands`` handler layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

_SKILL_DESC_MAX = 60  # skill descriptions can be long; the /help listing shows a short head

COMMANDS: dict[str, str] = {
    "/help": "show this help",
    "/clear": "clear the transcript",
    "/diff": "show the working-tree diff",
    "/undo": "revert the last change (or /undo list)",
    "/model": "show or switch the model (/model <name>)",
    "/autonomy": "show or set autonomy (plan|confirm|auto)",
    "/mcp": "manage MCP servers (list · add · enable · auth · tools; /mcp for the list)",
    "/sessions": "list sessions (or /sessions resume <id>)",
    "/new": "start a fresh session",
    "/output": "show a command's full output (/output [N]; or press ^O)",
    "/cost": "show usage & cost (/cost open for the dashboard)",
    "/debug": "write a shareable debug report of this session (to paste for help)",
    "/exit": "quit jack",
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What the UI shell should do in response to a command."""

    action: str  # "message" | "clear" | "exit"
    text: str = ""


def parse(line: str) -> tuple[str, str] | None:
    """Split a ``/``-prefixed line into ``(name, args)``; ``None`` if it isn't a command."""
    if not line.startswith("/"):
        return None
    head, _, rest = line.strip().partition(" ")
    return head, rest.strip()


def classify_line(line: str, skill_names: frozenset[str]) -> tuple[str, str, str]:
    """Classify a submitted line as a command, skill invocation, unknown slash, or prose.

    Only a line whose *whole* text begins with ``/`` is a directive — mid-line slashes are
    prose (and so can never trigger a control command like ``/exit`` by accident). A built-in
    command wins over a same-named skill (commands are the control surface).

    Args:
        line: The raw submitted text.
        skill_names: The bare names (no ``/``) of skills available for activation.

    Returns:
        ``(kind, name, args)`` where ``kind`` is one of ``"command"`` (``name`` keeps its
        ``/``), ``"skill"`` (``name`` is the bare skill name), ``"unknown"`` (a ``/foo`` that
        is neither), or ``"prose"`` (``name``/``args`` empty).
    """
    parsed = parse(line)
    if parsed is None:
        return ("prose", "", "")
    head, args = parsed
    if head in COMMANDS:
        return ("command", head, args)
    if head[1:] in skill_names:
        return ("skill", head[1:], args)
    return ("unknown", head, args)


def skill_nudge(name: str, args: str) -> str:
    """The turn text that activates a skill: an instruction to load and follow it.

    Keeps the model in charge of activation (it calls the ``skill`` tool), so the permission
    gate and the rest of the turn flow are untouched. Any ``args`` the user typed after the
    ``/name`` are appended as the task.
    """
    base = f'Use the "{name}" skill for this — load it with skill("{name}"), then follow it.'
    return f"{base} {args}".strip() if args.strip() else base


def _one_line(text: str, limit: int = _SKILL_DESC_MAX) -> str:
    """Collapse a skill description to a single, length-capped line for the /help listing."""
    line = " ".join(text.split())  # fold newlines / runs so each skill stays one row
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _help_text(skills: Sequence[tuple[str, str]] = ()) -> str:
    """The ``/help`` listing: built-in commands, then any installed skills (shown as ``/name``).

    Skills are invoked exactly like commands (``/brainstorming``) but activate a skill instead,
    so listing them here — next to the completer's ``/`` menu — is the readable answer to "what
    can I run?". ``skills`` is the same ``(name, description)`` catalog the completer uses.
    """
    lines = ["Commands:", *(f"  {name}  {desc}" for name, desc in COMMANDS.items())]
    if skills:
        lines += ["", "Skills (type / to autocomplete):"]
        lines += [f"  /{name}  {_one_line(desc)}" for name, desc in skills]
    return "\n".join(lines)


def dispatch(name: str, args: str, skills: Sequence[tuple[str, str]] = ()) -> CommandResult:
    """Run a client-side command by name (``skills`` enriches the ``/help`` listing)."""
    if name == "/help":
        return CommandResult("message", _help_text(skills))
    if name == "/clear":
        return CommandResult("clear")
    if name == "/exit":
        return CommandResult("exit")
    return CommandResult("message", f"Unknown command: {name} (try /help)")
