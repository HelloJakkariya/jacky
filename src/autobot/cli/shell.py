"""The inline coding-agent CLI: one pinned-input prompt_toolkit app over the daemon turns.

Completed turns commit to native scrollback; a single live region shows in-flight activity;
esc interrupts. The heavy lifting lives in ``app`` (the Application + ``AppSurface``),
``driver`` (the async turn drive loop over the ``Surface`` seam), and ``live_region`` (the
frame composer). This module is the composition root: build the console + app, wire the turn
closure and background auto-resume, and run. The pure helpers (context banner, ``@``-mention
expansion, slash-command routing, session footer) are unit-tested without a TTY or a daemon.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

from autobot.cli import coder_commands, commands, debug_report, mentions
from autobot.logging_setup import get_logger

_log = get_logger("cli")


def gather_context(cwd: str) -> dict[str, str]:
    """Best-effort status context for the HUD (never raises)."""
    branch, dirty, ahead, behind = _git_status(cwd)
    model, autonomy, provider = "?", "?", ""
    try:
        from autobot.config import Settings

        s = Settings.load()
        provider = s.llm_provider
        model = s.anthropic_model if s.llm_provider == "anthropic" else s.llm_model
        autonomy = s.coding_autonomy
    except Exception:  # config is best-effort for a status line
        _log.debug("could not load settings for context", exc_info=True)
    return {
        "cwd": _short(cwd),
        "branch": branch,
        "model": model,
        "autonomy": autonomy,
        "provider": provider,
        "dirty": "1" if dirty else "",
        "ahead": str(ahead),
        "behind": str(behind),
    }


def _branch(cwd: str) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _git_status(cwd: str) -> tuple[str, bool, int, int]:
    """Return ``(branch, dirty, ahead, behind)`` best-effort (empty/zero on any failure)."""
    import re

    branch = _branch(cwd)
    if not branch:
        return "", False, 0, 0
    dirty, ahead, behind = False, 0, 0
    try:
        st = subprocess.run(
            ["git", "status", "--porcelain", "--branch"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return branch, False, 0, 0
    if st.returncode == 0:
        lines = st.stdout.splitlines()
        dirty = any(not ln.startswith("##") for ln in lines)
        header = lines[0] if lines else ""
        m_ahead = re.search(r"ahead (\d+)", header)
        m_behind = re.search(r"behind (\d+)", header)
        ahead = int(m_ahead.group(1)) if m_ahead else 0
        behind = int(m_behind.group(1)) if m_behind else 0
    return branch, dirty, ahead, behind


def _short(cwd: str) -> str:
    from pathlib import Path

    home = str(Path.home())
    return cwd.replace(home, "~", 1) if cwd.startswith(home) else cwd


def _seed_hud_counts(japp: Any, base_url: str) -> None:
    """Seed best-effort HUD counts (MCP servers) at startup; never raises."""
    from autobot.cli import mcp_client

    try:
        payload = mcp_client.list_servers(base_url)
        servers = payload.get("servers")
        if isinstance(servers, list):  # absent/error payloads leave the count unset (omitted)
            japp.update_hud(mcp_count=len(servers))
    except Exception:  # counts are best-effort
        _log.debug("could not seed mcp count", exc_info=True)


def _refresh_hud_after_turn(japp: Any, base_url: str, cwd: str) -> None:
    """Post-turn: refresh context %, cost, and git so the HUD reflects this turn.

    Pulls from ``GET /coder/usage`` — the ``ctx`` block carries the live context-window
    ``used``/``window``/``model`` (the same payload the orb's meter uses), and
    ``rollups.session.usd`` the session cost. Best-effort: never raises out of a turn.
    """
    from autobot.cli import client

    try:
        usage = client.get_usage(base_url)
        ctx = usage.get("ctx") or {}
        if ctx:
            japp.on_context_event(ctx)  # used/window/model → context% + tokens segments
        session = (usage.get("rollups") or {}).get("session") or {}
        usd = session.get("usd")
        fresh = gather_context(cwd)
        japp.update_hud(
            cost_usd=float(usd) if usd is not None else japp.hud_state.cost_usd,
            branch=fresh["branch"],
            dirty=bool(fresh.get("dirty")),
            ahead=int(fresh.get("ahead", "0") or 0),
            behind=int(fresh.get("behind", "0") or 0),
        )
    except Exception:  # the HUD refresh must never break a turn
        _log.debug("post-turn HUD refresh failed", exc_info=True)


def expand_mentions(text: str, cwd: str) -> tuple[str, list[str]]:
    """Resolve ``@path`` mentions into bounded file context.

    Returns ``(resolved_text, attached_names)``; the text is unchanged when nothing matched.
    """
    attached = mentions.find_mentions(text)
    if not attached:
        return text, []
    return mentions.resolve_mentions(text, cwd), attached


def skill_catalog(cwd: str) -> list[tuple[str, str]]:
    """Snapshot installed skills as ``(name, description)`` for the ``/`` completer.

    Built once at startup from the same on-disk registry ``jack skills`` uses (no daemon
    round-trip). Never raises — a broken skill dir just yields an empty catalog. A skill
    authored mid-session appears after a restart; the model still sees it live via its catalog.
    """
    try:
        from pathlib import Path

        from autobot.skills.registry import SkillRegistry, default_skill_dirs

        registry = SkillRegistry(default_skill_dirs(Path.home(), Path(cwd)))
        return [(spec.name, spec.description) for spec in registry.specs()]
    except Exception:  # completion is best-effort; a bad skill dir must not block the shell
        _log.debug("could not load skill catalog for completion", exc_info=True)
        return []


def route_command(
    name: str,
    args: str,
    *,
    base_url: str,
    cwd: str,
    width: int,
    skills: Sequence[tuple[str, str]] = (),
) -> tuple[Any | None, str]:
    """Route a slash command; return ``(renderable_or_text | None, action)``.

    ``action`` is ``""`` (just render the first element), ``"clear"``, or ``"exit"``.
    Daemon-backed commands (``/diff`` …) go through ``coder_commands.handle`` first; the
    client-side trio (``/help /clear /exit``) falls back to ``commands.dispatch``. ``skills``
    is threaded to ``dispatch`` so ``/help`` can list the installed skills alongside commands.
    """
    rendered = coder_commands.handle(name, args, base_url=base_url, cwd=cwd, width=width)
    if rendered is not None:
        return rendered, ""
    res = commands.dispatch(name, args, skills)
    if res.action == "exit":
        return None, "exit"
    if res.action == "clear":
        return None, "clear"
    return res.text, ""


def print_session_footer(console: Any, cwd: str, turns: int) -> None:
    """On exit, point at the transcript + how to get a shareable debug report.

    Only after real work (≥1 turn) — so just opening and quitting stays quiet.
    """
    if turns == 0:
        return
    from rich.text import Text

    transcript = debug_report.newest_transcript(cwd)
    console.print()
    if transcript is not None:
        console.print(Text(f"session transcript: {transcript}", style="dim"))
    console.print(
        Text(
            "stuck or something off? run  jack debug  here for a shareable report to paste.",
            style="dim",
        )
    )


def run(base_url: str, cwd: str) -> None:  # pragma: no cover - launches the interactive app
    """Launch the pinned-input coding-agent app against ``base_url`` for ``cwd``."""
    import asyncio
    from pathlib import Path

    from prompt_toolkit.history import FileHistory
    from rich.console import Console
    from rich.text import Text

    from autobot.cli import client, gitdiff, render, theme
    from autobot.cli.app import AppSurface, JackApp
    from autobot.cli.autoresume import BackgroundEvents
    from autobot.cli.driver import TurnDriver, aiter_blocking
    from autobot.cli.theme import jack_theme

    console = Console(theme=jack_theme())  # only for the post-exit session footer
    context = gather_context(cwd)
    skills = skill_catalog(cwd)  # (name, description) snapshot for the / completer + ghost text
    skill_names = frozenset(name for name, _ in skills)
    hist_dir = Path(cwd) / ".jack"
    hist_dir.mkdir(parents=True, exist_ok=True)  # persist ↑/↓ input history per workspace
    history = FileHistory(str(hist_dir / "cli_history"))
    events = BackgroundEvents(base_url)
    holder: dict[str, JackApp] = {}

    async def run_turn(text: str, turn_no: int) -> None:
        japp = holder["app"]
        surface = AppSurface(japp)
        # Echo the user's turn as its own block; the transcript composer supplies the blank
        # lines that separate it from the previous turn and from the response below.
        surface.commit(Text(f"{theme.GLYPH_PROMPT} {text}", style="prompt"))
        kind, name, cargs = commands.classify_line(text, skill_names)
        turn_text = text
        if kind in ("command", "unknown"):
            if name == "/mcp":
                from autobot.cli import mcp_repl

                await mcp_repl.handle(cargs, surface, base_url=base_url)
                return
            if name == "/output":  # client-side: expand a stashed command's output
                arg = cargs.strip()
                if not japp.expand_output(int(arg) if arg.isdigit() else None):
                    surface.commit(Text("No command output to show yet.", style="dim"))
                return
            out, action = route_command(
                name, cargs, base_url=base_url, cwd=cwd, width=japp._cols(), skills=skills
            )
            if action == "exit":
                japp._exiting = True
                japp.app.exit()
                return
            if action == "clear":
                japp.clear_transcript()
                return
            if out is not None:
                surface.commit(out)
            return
        if kind == "skill":  # /skill-name → activate it via the model's skill tool (a nudge turn)
            _log.info("slash skill activated name=%r", name)
            turn_text = commands.skill_nudge(name, cargs)
        resolved, attached = expand_mentions(japp.expand_pastes(turn_text), cwd)
        if attached:
            surface.commit(
                Text(f"{theme.NEST_INDENT}attached: {', '.join(attached)}", style="tool")
            )
        driver = TurnDriver(
            surface, cwd=cwd, snapshot=gitdiff.snapshot, diff_since=gitdiff.diff_since
        )

        def answer_stream(value: str, atext: str) -> Any:
            return aiter_blocking(client.stream_answer(base_url, value, atext))

        await driver.run_turn(
            aiter_blocking(client.stream_turn(base_url, resolved)),
            answer_stream,
            turn_no=turn_no,
        )
        _refresh_hud_after_turn(japp, base_url, cwd)  # cost + git reflect the turn just finished

    japp = JackApp(
        cwd=cwd,
        run_turn=run_turn,
        commands=commands.COMMANDS,
        skills=skills,
        context=context,
        intro=render.render_welcome(context),
        on_interrupt=lambda: client.coder_interrupt(base_url),
        history=history,
    )
    holder["app"] = japp

    def _waker() -> None:
        # Fired on the events-listener thread. Only drain once the app loop exists, so a task
        # finishing during startup isn't drained-then-dropped; on_task_finished hops to the loop.
        if japp.app.loop is not None:
            japp.on_task_finished(events.poll_completed())

    events.set_waker(_waker)
    events.set_on_mcp(japp.on_mcp_event)
    # Seed the git ahead/behind + dirty flags (gather_context already put branch/model/etc into
    # the app's HudState) and the best-effort MCP count, so the bar is populated before turn one.
    japp.update_hud(
        dirty=bool(context.get("dirty")),
        ahead=int(context.get("ahead", "0") or 0),
        behind=int(context.get("behind", "0") or 0),
    )
    _seed_hud_counts(japp, base_url)
    events.start()
    try:
        asyncio.run(japp.run_async())
    finally:
        events.close()
        print_session_footer(console, cwd, japp.turn_no)
