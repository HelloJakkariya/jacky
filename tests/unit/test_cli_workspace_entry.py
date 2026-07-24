"""The `jack` entry point resolves the workspace and routes `jack restart`."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.cli import main as cli_main
from autobot.cli import resolve_workspace


def test_resolve_workspace_prefers_arg(tmp_path: Path) -> None:
    other = tmp_path / "other"
    assert resolve_workspace(tmp_path, str(other)) == other.resolve()
    assert resolve_workspace(tmp_path, None) == tmp_path.resolve()


def test_jack_restart_calls_stop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called = {"stopped": False}

    def fake_stop(ws: str, **_k: object) -> bool:
        called["stopped"] = True
        return True

    monkeypatch.setattr("autobot.cli.stop_workspace", fake_stop)
    rc = cli_main(["restart"])
    assert rc == 0 and called["stopped"] is True
    assert "stopped" in capsys.readouterr().out.lower()


def test_jack_trust_records_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    trusted: list[str] = []
    monkeypatch.setattr("autobot.trust.add_trust", lambda folder: trusted.append(str(folder)))
    monkeypatch.chdir(tmp_path)
    rc = cli_main(["trust"])
    assert rc == 0
    assert trusted == [str(tmp_path.resolve())]
    assert "trusted" in capsys.readouterr().out.lower()


def test_turn_aborts_when_untrusted_and_noninteractive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Untrusted + no TTY (pytest stdin isn't a tty) → refuse to act (don't reach the daemon).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("autobot.trust.is_trusted", lambda folder: False)
    reached = {"daemon": False}

    def fake_ensure(*_a: object, **_k: object) -> None:
        reached["daemon"] = True

    monkeypatch.setattr("autobot.cli.ensure_daemon", fake_ensure)
    rc = cli_main(["-p", "do a thing"])
    assert rc == 1 and reached["daemon"] is False
    assert "not a trusted workspace" in capsys.readouterr().err.lower()
