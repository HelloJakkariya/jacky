"""The shell composition root's pure helpers: context banner, mentions, command routing, footer.

The interactive drive loop now lives in the async app (test_cli_app) and the turn driver
(test_cli_driver); this file covers the small, TTY-free helpers shell.run wires together.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from autobot.cli import shell
from autobot.cli.theme import jack_theme


def _console() -> Console:
    return Console(record=True, width=80, theme=jack_theme(), force_terminal=True)


def test_gather_context_never_raises_and_has_keys(tmp_path: Path) -> None:
    ctx = shell.gather_context(str(tmp_path))
    assert set(ctx) == {
        "cwd",
        "branch",
        "model",
        "autonomy",
        "provider",
        "dirty",
        "ahead",
        "behind",
    }


def test_refresh_hud_after_turn_pulls_ctx_and_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput

    from autobot.cli import client
    from autobot.cli.app import JackApp

    async def noop(_t: str, _n: int) -> None:
        return None

    japp = JackApp(
        cwd=str(tmp_path), run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput()
    )

    def fake_usage(_base_url: str, **_kw: object) -> dict[str, object]:
        return {
            "ctx": {"used": 50000, "window": 200000, "model": "opus"},
            "rollups": {"session": {"usd": 0.34}},
        }

    monkeypatch.setattr(client, "get_usage", fake_usage)
    shell._refresh_hud_after_turn(japp, "http://x", str(tmp_path))

    assert japp.hud_state.used == 50000
    assert japp.hud_state.window == 200000
    assert japp.hud_state.model == "opus"
    assert japp.hud_state.cost_usd == 0.34


def test_expand_mentions_no_mention_is_identity(tmp_path: Path) -> None:
    text, attached = shell.expand_mentions("just a plain request", str(tmp_path))
    assert text == "just a plain request"
    assert attached == []


def test_expand_mentions_resolves_a_file(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello from the file", encoding="utf-8")
    text, attached = shell.expand_mentions("read @note.txt please", str(tmp_path))
    assert attached == ["note.txt"]
    assert "hello from the file" in text  # the file body was folded into the text


def test_route_command_help_renders(tmp_path: Path) -> None:
    out, action = shell.route_command("/help", "", base_url="http://x", cwd=str(tmp_path), width=80)
    assert action == ""
    assert out is not None and "/exit" in str(out)


def test_route_command_help_lists_skills(tmp_path: Path) -> None:
    # The startup skill catalog is threaded into /help so it lists skills next to commands.
    out, action = shell.route_command(
        "/help",
        "",
        base_url="http://x",
        cwd=str(tmp_path),
        width=80,
        skills=[("brainstorming", "Explore ideas.")],
    )
    assert action == "" and out is not None and "/brainstorming" in str(out)


def test_route_command_exit_signals_exit(tmp_path: Path) -> None:
    out, action = shell.route_command("/exit", "", base_url="http://x", cwd=str(tmp_path), width=80)
    assert action == "exit" and out is None


def test_route_command_clear_signals_clear(tmp_path: Path) -> None:
    out, action = shell.route_command(
        "/clear", "", base_url="http://x", cwd=str(tmp_path), width=80
    )
    assert action == "clear" and out is None


def test_route_command_daemon_backed_goes_through_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autobot.cli import coder_commands

    monkeypatch.setattr(
        coder_commands, "handle", lambda name, *a, **k: "HANDLED" if name == "/diff" else None
    )
    out, action = shell.route_command("/diff", "", base_url="http://x", cwd=str(tmp_path), width=80)
    assert action == "" and out == "HANDLED"


def test_print_session_footer_quiet_when_no_turns(tmp_path: Path) -> None:
    console = _console()
    shell.print_session_footer(console, str(tmp_path), turns=0)
    assert console.export_text().strip() == ""  # nothing printed for an empty session


def test_print_session_footer_points_at_debug_after_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autobot.cli import debug_report

    monkeypatch.setattr(debug_report, "newest_transcript", lambda _cwd: None)
    console = _console()
    shell.print_session_footer(console, str(tmp_path), turns=2)
    assert "jack debug" in console.export_text()
