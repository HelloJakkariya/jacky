"""`jack` — terminal client for the coder daemon (one-shot today; TUI in cli.tui)."""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from difflib import get_close_matches
from pathlib import Path

from autobot.cli.client import (
    _CODER_PORT,
    _SPAWN_TIMEOUT_S,
    _log_tail,
    _post,
    _probe,
    _prompt_user,
    ensure_daemon,
    is_daemon_up,
    list_daemons,
    run_coder_turn,
    stop_all_daemons,
    stop_workspace,
    stream_answer,
    stream_turn,
    workspace_port,
)

__all__ = [
    "_CODER_PORT",
    "_SPAWN_TIMEOUT_S",
    "_log_tail",
    "_post",
    "_probe",
    "_prompt_user",
    "_run_cost",
    "_run_debug",
    "ensure_daemon",
    "is_daemon_up",
    "list_daemons",
    "main",
    "resolve_workspace",
    "run_coder_turn",
    "stop_all_daemons",
    "stop_workspace",
    "stream_answer",
    "stream_turn",
    "workspace_port",
]


def resolve_workspace(cwd: Path, arg: str | None) -> Path:
    """The coder workspace: an explicit ``--workspace`` wins, else the launch cwd."""
    return (Path(arg).expanduser() if arg else cwd).resolve()


def _trust_workspace(ws: Path) -> None:
    """Mark ``ws`` trusted and create its ``.jack/`` home."""
    from autobot.trust import add_trust

    add_trust(ws)
    (ws / ".jack").mkdir(parents=True, exist_ok=True)


def _ensure_trusted(ws: Path) -> bool:
    """Prompt once to trust ``ws`` (persisting on yes). Return True if it's OK to proceed.

    An untrusted folder is never acted in: interactively we ask; non-interactively we refuse
    with a hint to run ``jack trust`` (so a piped/CI run can't silently act in an untrusted
    directory).
    """
    from autobot.trust import is_trusted

    if is_trusted(ws):
        return True
    if not sys.stdin.isatty():
        print(
            f"{ws} is not a trusted workspace. Run `jack trust` here to let Jack read, "
            "write, and run commands in it.",
            file=sys.stderr,
        )
        return False
    answer = input(f"Trust this folder? Jack can read, write, and run commands in\n  {ws}\n[y/N] ")
    if answer.strip().lower().startswith("y"):
        _trust_workspace(ws)
        return True
    print("Not trusted — aborting.", file=sys.stderr)
    return False


def _launch_editor(path: str) -> int:
    """Open ``path`` in ``$EDITOR`` (fallback: ``open -t`` on macOS)."""
    editor = os.environ.get("EDITOR")
    if editor:
        return subprocess.call([*editor.split(), path])
    if sys.platform == "darwin":
        return subprocess.call(["open", "-t", path])
    print("set $EDITOR to edit; the file is at:", file=sys.stderr)
    print(path, file=sys.stderr)
    return 1


def _run_config(action: str, rest: list[str], base_url: str) -> int:
    """Build real dependencies and dispatch a ``jack config`` action.

    Writes target the current workspace's ``.jack/settings.json`` by default; ``--global``
    targets ``~/.autobot/settings.json``. Reads show the merged (workspace-over-global) view.
    """
    from getpass import getpass

    from autobot.cli.config_cmd import Deps, run
    from autobot.config import DEFAULT_SETTINGS_PATH
    from autobot.secrets import delete_secret, get_secret, set_secret

    use_global = "--global" in rest
    rest = [a for a in rest if a != "--global"]
    workspace_settings = resolve_workspace(Path.cwd(), None) / ".jack" / "settings.json"
    global_path = Path(DEFAULT_SETTINGS_PATH).expanduser()
    deps = Deps(
        settings_path=global_path if use_global else workspace_settings,
        global_path=global_path,
        workspace_settings=workspace_settings,
        base_url=base_url,
        set_secret=set_secret,
        delete_secret=delete_secret,
        get_secret=get_secret,
        prompt_secret=getpass,
        launch_editor=_launch_editor,
    )
    return run(action, rest, deps)


def _run_cost(rest: list[str]) -> int:
    """`jack cost` — cross-project usage from the global ledger (no daemon needed).

    `--open` writes + opens the HTML dashboard; otherwise prints a terminal summary.
    """
    from datetime import datetime

    from rich.console import Console

    from autobot.cli import render
    from autobot.config import Settings
    from autobot.usage import ledger, rollup

    rolls = rollup.summarize(ledger.read(), now=datetime.now(), session_id=None).to_dict()
    if "--open" in rest:
        from autobot.usage.report import write_and_open

        path = write_and_open(rolls, now=datetime.now())
        print(f"Opened the usage report ({path}).")
        return 0
    payload = {
        "ctx": None,
        "provider": Settings.load().llm_provider,
        "model": None,
        "rollups": rolls,
    }
    Console().print(render.render_cost(payload, 100))
    return 0


def _run_debug() -> int:
    """`jack debug` — write a shareable session debug bundle (works with or without a daemon).

    Reuses the in-REPL ``/debug`` assembler: the daemon's redacted report if it's up, else a
    log-file report; plus the newest transcript + session usage. Handy right after cancelling a
    stuck turn.
    """
    from autobot.cli import coder_commands

    ws = resolve_workspace(Path.cwd(), None)
    base_url = f"http://127.0.0.1:{workspace_port(str(ws))}"
    print(coder_commands._debug(base_url, str(ws), coder_commands.Deps()))
    return 0


# The user-facing subcommands, in help-display order. `serve` is intentionally omitted — it's
# an internal entry point used by the frozen re-exec, not something a user runs. This table is
# the single source of truth for `jack help` and the unknown-command suggestions, so the
# advertised commands can never drift from what dispatch actually accepts.
_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("config", "show or change settings"),
    ("skills", "list, add, or inspect skills"),
    ("workflows", "list, show, or run workflows"),
    ("mcp", "manage MCP servers (list · add · enable · auth · tools)"),
    ("cost", "show usage & cost (cost --open for the dashboard)"),
    ("debug", "write a shareable debug report of this session"),
    ("daemons", "list running coder daemons"),
    ("restart", "restart this workspace's coder daemon"),
    ("stop", "stop this workspace's daemon (stop --all for all)"),
    ("trust", "trust this folder so Jack can read, write & run here"),
    ("update", "update the installed jack binary"),
    ("version", "print the jack version"),
)


def _usage_text() -> str:
    """The top-level `jack help` / `jack --help` listing, built from :data:`_SUBCOMMANDS`."""
    rows = "\n".join(f"  {name:<10} {summary}" for name, summary in _SUBCOMMANDS)
    return (
        "jack — a local coding agent in your terminal.\n"
        "\n"
        "Usage:\n"
        "  jack                     open the interactive TUI in this workspace\n"
        '  jack -p "<request>"      run a single request, print the reply, and exit\n'
        "  jack <command> [args]    run one of the commands below\n"
        "\n"
        f"Commands:\n{rows}\n"
        "\n"
        "Run 'jack <command> help' for a command's own options (e.g. jack config help)."
    )


def _unknown_command(argv: list[str]) -> int:
    """Report a bare word that isn't a subcommand — never run it as an agent turn.

    Shows a "did you mean" for a near-miss, points at ``jack help``, and reminds the user that a
    free-form request goes through ``-p`` — so ``jack sessions`` teaches instead of silently
    executing. Returns the conventional usage exit code (2); touches neither trust nor daemon.
    """
    cmd = argv[0]
    lines = [f"jack: unknown command {cmd!r}."]
    # cutoff 0.75: real typos ("skils"→"skills") score ≥0.9, while weak overlaps
    # ("sessions"→"version" is 0.67) fall away — a misleading suggestion is worse than none.
    close = get_close_matches(cmd.split()[0], [name for name, _ in _SUBCOMMANDS], n=1, cutoff=0.75)
    if close:
        lines.append(f"Did you mean '{close[0]}'?")
    lines.append("Run 'jack help' to see all commands.")
    lines.append(f'To send this as a request:  jack -p "{" ".join(argv)}"')
    print("\n".join(lines), file=sys.stderr)
    return 2


def _split_prompt(argv: list[str]) -> tuple[str | None, list[str]]:
    """Split a flags-mode argv into ``(request, leading-flags)``.

    ``-p``/``--prompt`` marks a one-shot request: everything after it is the request text (so both
    ``jack -p "fix the bug"`` and ``jack -p fix the bug`` work), and any flags before it
    (``--port``, ``--workspace``) stay for the parser. With no ``-p`` there is no request, and the
    caller opens the TUI instead.
    """
    for i, token in enumerate(argv):
        if token in ("-p", "--prompt"):
            request = " ".join(argv[i + 1 :]).strip()
            return (request or None), argv[:i]
    return None, argv


def main(argv: list[str] | None = None) -> int:
    """`jack` opens the TUI; `jack -p "…"` runs a request; `jack help` lists all commands."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V", "version"):
        from autobot import __version__

        print(f"jack {__version__}")
        return 0
    if argv and argv[0] in ("help", "--help", "-h"):
        print(_usage_text())  # discovery: list every command before any daemon/trust work
        return 0
    if argv and argv[0] == "serve":
        from autobot.daemon.__main__ import main as daemon_main

        daemon_main(argv[1:])  # run the headless daemon (used by the frozen re-exec)
        return 0
    if argv and argv[0] == "config":
        action = argv[1] if len(argv) > 1 else "show"
        return _run_config(action, argv[2:], f"http://127.0.0.1:{_CODER_PORT}")
    if argv and argv[0] == "skills":
        from autobot.cli.skills_cmd import run as skills_run

        return skills_run(argv[1:])
    if argv and argv[0] == "workflows":
        from autobot.cli.workflows_cmd import run as workflows_run

        return workflows_run(argv[1:])
    if argv and argv[0] == "mcp":
        from autobot.cli.mcp_cmd import run as mcp_run

        ws = resolve_workspace(Path.cwd(), None)
        if not _ensure_trusted(ws):
            return 1
        port = workspace_port(str(ws))
        base_url = f"http://127.0.0.1:{port}"
        try:
            ensure_daemon(base_url, port, workspace=str(ws))
        except (RuntimeError, TimeoutError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return mcp_run(argv[1:], base_url=base_url)
    if argv and argv[0] == "cost":
        return _run_cost(argv[1:])
    if argv and argv[0] == "debug":
        return _run_debug()
    if argv and argv[0] == "restart":
        ws = resolve_workspace(Path.cwd(), None)
        stopped = stop_workspace(str(ws))
        print(f"coder daemon stopped for {ws}." if stopped else "no coder daemon was running here.")
        return 0
    if argv and argv[0] == "daemons":
        rows = list_daemons()
        if not rows:
            print("no coder daemons running.")
        for r in rows:
            print(f"{'up  ' if r['up'] else 'dead'}  :{r['port']}  {r['workspace']}")
        return 0
    if argv and argv[0] == "stop":
        if "--all" in argv[1:]:
            print(f"stopped {stop_all_daemons()} coder daemon(s).")
        else:
            ws = resolve_workspace(Path.cwd(), None)
            stopped = stop_workspace(str(ws))
            print(f"stopped the coder daemon for {ws}." if stopped else "none running here.")
        return 0
    if argv and argv[0] == "trust":
        target = resolve_workspace(Path.cwd(), argv[1] if len(argv) > 1 else None)
        _trust_workspace(target)
        print(f"trusted: {target}")
        return 0
    if argv and argv[0] == "update":
        from autobot import __version__, update

        if not getattr(sys, "frozen", False):
            # sys.executable is the frozen `jack` binary only in a packaged install; from a
            # source checkout it's the venv Python, which self-replace must never overwrite.
            print(
                "jack update replaces the installed binary; from a source checkout, "
                "update with git/uv instead.",
                file=sys.stderr,
            )
            return 1
        try:
            print(update.run_update(__version__, Path(sys.executable)))
            return 0
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    # Fallthrough: argv[0] is not a known subcommand. A bare word is a mistyped/unknown command —
    # teach with `jack help`, never silently run it as an agent turn (that's what `-p` is for).
    # Anything else is flags mode: `-p "…"` one-shot request, otherwise open the TUI.
    if argv and not argv[0].startswith("-"):
        return _unknown_command(argv)
    prompt, rest = _split_prompt(argv)
    parser = argparse.ArgumentParser(prog="jack", description="Jack coding agent (terminal).")
    parser.add_argument(
        "--port", type=int, default=None, help="coder daemon port (default: per-workspace)"
    )
    parser.add_argument("--workspace", default=None, help="workspace dir (default: cwd)")
    args = parser.parse_args(rest)
    ws = resolve_workspace(Path.cwd(), args.workspace)
    if not _ensure_trusted(ws):
        return 1
    port = args.port if args.port is not None else workspace_port(str(ws))
    base_url = f"http://127.0.0.1:{port}"
    try:
        ensure_daemon(base_url, port, workspace=str(ws))
        print(f"workspace: {ws}", file=sys.stderr)
        _maybe_backend_hint()
        if prompt is not None:
            print(run_coder_turn(base_url, prompt))
        else:
            import autobot.cli.tui as tui

            tui.run(base_url, str(ws))
    except (RuntimeError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ImportError:
        print(
            "The interactive TUI needs rich + prompt_toolkit (base deps) — "
            "run `uv sync` (or reinstall jacky) to repair the environment.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        # Best-effort: unblock a worker parked awaiting a reply; never fail on this.
        with contextlib.suppress(Exception):
            _post(f"{base_url}/coder/reply", {"value": "reject"}, 1.0)
        return 130
    _print_update_notice()
    return 0


def _maybe_backend_hint() -> None:
    """Best-effort first-run nudge when no LLM backend is set up (never raises).

    A failed reachability probe means "not reachable", not "abort the whole hint" —
    so it's caught locally rather than by the outer ``suppress``.
    """
    with contextlib.suppress(Exception):
        from autobot import update
        from autobot.config import Settings
        from autobot.secrets import get_secret

        s = Settings.load()
        has_key = bool(get_secret("anthropic_api_key") or get_secret("openai_api_key"))
        try:
            ollama_up = _probe("http://127.0.0.1:11434", 0.3)
        except Exception:
            ollama_up = False
        hint = update.backend_hint(s.llm_provider, has_key, ollama_up)
        if hint:
            print(hint, file=sys.stderr)


def _print_update_notice() -> None:
    """Best-effort: one dim line if a newer release exists (throttled to once/day)."""
    import time

    from autobot import __version__, update

    # Runs last, after main()'s KeyboardInterrupt handler, and does a blocking network
    # fetch. A Ctrl+C here should exit cleanly, not dump a traceback — and since
    # KeyboardInterrupt is a BaseException, plain suppress(Exception) would miss it.
    with contextlib.suppress(Exception, KeyboardInterrupt):
        latest = update.check_for_update(
            __version__, time.time(), update.cache_path(), update.fetch_latest_version
        )
        notice = update.update_notice(latest)
        if notice:
            print(notice, file=sys.stderr)
