"""Top-level `jack` discovery: `jack help`, unknown-command errors, and the `-p` prompt flag.

These pin the Claude-style dispatch contract: subcommands are first-class, a bare unknown
word is never silently run as an agent turn, and a one-shot request goes through `-p`.
"""

from __future__ import annotations

import pytest

import autobot.cli as cli


@pytest.fixture(autouse=True)
def _auto_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the workspace as trusted; these tests exercise dispatch, not the trust gate."""
    monkeypatch.setattr(cli, "_ensure_trusted", lambda ws: True)


def test_help_lists_every_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["help"]) == 0
    out = capsys.readouterr().out
    for name, _summary in cli._SUBCOMMANDS:
        assert name in out, f"{name} missing from `jack help`"
    assert 'jack -p "' in out  # the one-shot prompt mode is documented
    assert "jack" in out  # the bare-TUI mode is documented


@pytest.mark.parametrize("flag", ["help", "--help", "-h"])
def test_help_flags_are_equivalent(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([flag]) == 0
    assert "Commands:" in capsys.readouterr().out


def test_unknown_command_errors_without_touching_daemon(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `jack sessions` must not spin up the daemon or send an agent turn — it's a typo, not a task.
    reached = {"daemon": False, "trust": False}

    def mark_daemon(*_a: object, **_k: object) -> None:
        reached["daemon"] = True

    def mark_trust(_ws: object) -> bool:
        reached["trust"] = True
        return True

    monkeypatch.setattr(cli, "ensure_daemon", mark_daemon)
    monkeypatch.setattr(cli, "_ensure_trusted", mark_trust)
    monkeypatch.setattr(cli, "run_coder_turn", lambda *a, **k: pytest.fail("agent turn ran"))

    rc = cli.main(["sessions"])

    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown command" in err.lower() and "sessions" in err
    assert "jack help" in err
    assert reached == {"daemon": False, "trust": False}


def test_unknown_command_suggests_a_close_match(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["skils"]) == 2
    assert "skills" in capsys.readouterr().err


def test_unknown_command_omits_weak_suggestions(capsys: pytest.CaptureFixture[str]) -> None:
    # `sessions` has no near-match; a misleading "did you mean version?" is worse than silence.
    assert cli.main(["sessions"]) == 2
    err = capsys.readouterr().err
    assert "did you mean" not in err.lower()
    assert "jack help" in err  # still points the user at discovery


def test_unknown_command_hints_the_prompt_flag(capsys: pytest.CaptureFixture[str]) -> None:
    # A prose-looking mistake should point the user at the explicit one-shot form.
    assert cli.main(["do", "a", "thing"]) == 2
    err = capsys.readouterr().err
    assert "-p" in err and "do a thing" in err


def test_dash_p_runs_a_one_shot_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "ensure_daemon", lambda *a, **k: None)
    monkeypatch.setattr(cli, "run_coder_turn", lambda base, text, **k: f"ran:{text}")
    assert cli.main(["-p", "fix the bug"]) == 0
    assert "ran:fix the bug" in capsys.readouterr().out


def test_dash_p_joins_the_remaining_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    # Everything after -p is the request, so unquoted multi-word prompts work too.
    seen: dict[str, str] = {}
    monkeypatch.setattr(cli, "ensure_daemon", lambda *a, **k: None)
    monkeypatch.setattr(
        cli, "run_coder_turn", lambda base, text, **k: seen.setdefault("text", text) or "ok"
    )
    cli.main(["--port", "9001", "-p", "fix", "the", "bug"])
    assert seen["text"] == "fix the bug"
