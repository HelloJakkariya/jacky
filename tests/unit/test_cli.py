from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest

import autobot.cli as cli


@pytest.fixture(autouse=True)
def _auto_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests exercise CLI plumbing, not the trust gate — treat the workspace trusted."""
    monkeypatch.setattr(cli, "_ensure_trusted", lambda ws: True)


def test_daemon_up_probe() -> None:
    # is_daemon_up returns True when the readiness probe succeeds, False on connection error.
    assert cli.is_daemon_up("http://x", probe=lambda url, timeout: True) is True

    def raising_probe(url: str, timeout: float) -> bool:
        raise OSError

    assert cli.is_daemon_up("http://x", probe=raising_probe) is False


def test_parse_sse_yields_event_dicts() -> None:
    from autobot.cli.client import _parse_sse

    lines = [
        'data: {"type": "tool", "event": "start", "name": "read_file", "label": "Read a"}',
        "",
        'data: {"status": "done", "reply": "ok"}',
        "",
    ]
    events = list(_parse_sse(iter(lines)))
    assert events[0]["type"] == "tool"
    assert events[1] == {"status": "done", "reply": "ok"}


_OpenStream = Callable[[str, dict[str, Any]], Iterator[str]]
_Call = tuple[str, dict[str, Any]]


def _sse_lines(events: list[dict[str, Any]]) -> list[str]:
    """Render event dicts as ``data: {json}`` SSE lines, each followed by a blank line."""
    lines: list[str] = []
    for evt in events:
        lines.append(f"data: {json.dumps(evt)}")
        lines.append("")
    return lines


def _scripted_stream(
    script: dict[str, list[dict[str, Any]]],
) -> tuple[_OpenStream, list[_Call]]:
    """Return a fake open_stream() that replays scripted SSE events per path."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def open_stream(url: str, payload: dict[str, Any]) -> Iterator[str]:
        calls.append((url, payload))
        path = "/coder/turn" if url.endswith("/coder/turn") else "/coder/reply"
        return iter(_sse_lines(script[path]))

    return open_stream, calls


def test_run_coder_turn_plan_approve_done() -> None:
    open_stream, calls = _scripted_stream(
        {
            "/coder/turn": [{"status": "plan", "reply": "1. edit foo", "todo": ["edit foo"]}],
            "/coder/reply": [{"status": "done", "reply": "Edited foo."}],
        }
    )
    reply = cli.run_coder_turn(
        "http://x", "edit foo", open_stream=open_stream, prompt=lambda r: {"value": "approve"}
    )
    assert reply == "Edited foo."
    assert calls[0][0].endswith("/coder/turn")
    assert calls[1][0].endswith("/coder/reply")
    assert calls[1][1] == {"value": "approve", "text": ""}


def test_run_coder_turn_pending_command_yes() -> None:
    turn_calls = {"n": 0}

    def open_stream(url: str, payload: dict[str, Any]) -> Iterator[str]:
        if url.endswith("/coder/turn"):
            return iter(_sse_lines([{"status": "plan", "reply": "1. run tests"}]))
        turn_calls["n"] += 1
        if turn_calls["n"] == 1:
            return iter(_sse_lines([{"status": "pending", "kind": "command", "prompt": "Run?"}]))
        return iter(_sse_lines([{"status": "done", "reply": "Tests passed."}]))

    answers = iter([{"value": "approve"}, {"value": "yes"}])
    reply = cli.run_coder_turn(
        "http://x", "run tests", open_stream=open_stream, prompt=lambda r: next(answers)
    )
    assert reply == "Tests passed."


def test_run_coder_turn_reject() -> None:
    open_stream, _ = _scripted_stream(
        {
            "/coder/turn": [{"status": "plan", "reply": "1. edit foo", "todo": ["edit foo"]}],
            "/coder/reply": [{"status": "done", "reply": "Okay, I won't make any changes."}],
        }
    )
    reply = cli.run_coder_turn(
        "http://x", "edit", open_stream=open_stream, prompt=lambda r: {"value": "reject"}
    )
    assert "won't" in reply.lower()


def test_run_coder_turn_handles_connection_error() -> None:
    def open_stream(url: str, payload: dict[str, Any]) -> Iterator[str]:
        raise OSError("Connection refused")

    reply = cli.run_coder_turn(
        "http://x", "hi", open_stream=open_stream, prompt=lambda r: {"value": "approve"}
    )
    assert "couldn't reach" in reply.lower()


def test_run_coder_turn_over_stream_plan_approve_done() -> None:
    from autobot.cli.client import run_coder_turn

    scripted = {
        "/coder/turn": [
            'data: {"status": "plan", "reply": "1. edit foo", "todo": ["edit foo"]}',
            "",
        ],
        "/coder/reply": [
            'data: {"type": "tool", "event": "start", "name": "edit_file", "label": "Edited foo"}',
            "",
            'data: {"status": "done", "reply": "Edited foo."}',
            "",
        ],
    }

    def open_stream(url: str, payload: dict[str, object]) -> Iterator[str]:
        path = "/coder/turn" if url.endswith("/coder/turn") else "/coder/reply"
        return iter(scripted[path])

    reply = run_coder_turn(
        "http://x", "edit foo", open_stream=open_stream, prompt=lambda r: {"value": "approve"}
    )
    assert reply == "Edited foo."


def test_main_one_shot(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen_port: list[int] = []
    monkeypatch.setattr(cli, "ensure_daemon", lambda base, port, **_k: seen_port.append(port))
    monkeypatch.setattr(cli, "run_coder_turn", lambda base, text, **k: "the reply")
    rc = cli.main(["--port", "9001", "-p", "do a thing"])
    assert rc == 0
    assert "the reply" in capsys.readouterr().out
    assert seen_port == [9001]  # main() forwards --port to the daemon spawn


def test_main_returns_1_when_daemon_cannot_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(base: str, port: int, **_k: object) -> None:
        raise TimeoutError("coder daemon did not start")

    monkeypatch.setattr(cli, "ensure_daemon", boom)
    rc = cli.main(["-p", "do a thing"])
    assert rc == 1
    assert "did not start" in capsys.readouterr().err


def test_main_surfaces_daemon_startup_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # When the spawned daemon dies (e.g. missing extra), ensure_daemon raises RuntimeError
    # with the reason — main() must print it and exit 1, not hang or dump a traceback.
    def boom(base: str, port: int, **_k: object) -> None:
        raise RuntimeError("the coder daemon couldn't start (exit 1). ... needs the daemon extra")

    monkeypatch.setattr(cli, "ensure_daemon", boom)
    rc = cli.main(["-p", "do a thing"])
    assert rc == 1
    assert "daemon extra" in capsys.readouterr().err


def test_main_handles_ctrl_c_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Ctrl-C during startup/turn must exit cleanly (130), not raise KeyboardInterrupt.
    def interrupted(base: str, port: int, **_k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "ensure_daemon", interrupted)
    rc = cli.main(["-p", "do a thing"])
    assert rc == 130
    assert "cancel" in capsys.readouterr().err.lower()


def test_main_ctrl_c_sends_best_effort_reject(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Ctrl-C best-effort unblocks a worker parked awaiting a reply by POSTing a reject.
    def interrupted(base: str, port: int, **_k: object) -> None:
        raise KeyboardInterrupt

    calls: list[tuple[str, dict[str, object]]] = []

    def fake_post(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        calls.append((url, payload))
        return {"status": "done"}

    monkeypatch.setattr(cli, "ensure_daemon", interrupted)
    monkeypatch.setattr(cli, "_post", fake_post)
    rc = cli.main(["--port", "9001", "-p", "do a thing"])
    assert rc == 130
    assert "cancel" in capsys.readouterr().err.lower()
    assert calls == [("http://127.0.0.1:9001/coder/reply", {"value": "reject"})]


def test_main_ctrl_c_swallows_post_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The best-effort reject must never change the exit path if it fails.
    def interrupted(base: str, port: int, **_k: object) -> None:
        raise KeyboardInterrupt

    def boom_post(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        raise OSError("connection refused")

    monkeypatch.setattr(cli, "ensure_daemon", interrupted)
    monkeypatch.setattr(cli, "_post", boom_post)
    rc = cli.main(["-p", "do a thing"])
    assert rc == 130
    assert "cancel" in capsys.readouterr().err.lower()


def test_stream_turn_posts_text() -> None:
    seen: dict[str, Any] = {}

    def fake_open_stream(url: str, payload: dict[str, Any]) -> Iterator[str]:
        seen["url"] = url
        seen["payload"] = payload
        return iter(_sse_lines([{"status": "plan", "reply": "1. x", "todo": ["x"]}]))

    events = list(cli.stream_turn("http://x", "do it", open_stream=fake_open_stream))
    assert events[0]["status"] == "plan"
    assert seen["url"].endswith("/coder/turn") and seen["payload"] == {"text": "do it"}


def test_stream_answer_posts_value_and_text() -> None:
    seen: dict[str, Any] = {}

    def fake_open_stream(url: str, payload: dict[str, Any]) -> Iterator[str]:
        seen["url"] = url
        seen["payload"] = payload
        return iter(_sse_lines([{"status": "done", "reply": "ok"}]))

    list(cli.stream_answer("http://x", "refine", "use bash", open_stream=fake_open_stream))
    assert seen["url"].endswith("/coder/reply")
    assert seen["payload"] == {"value": "refine", "text": "use bash"}


def test_stream_turn_maps_connection_error_to_error_event() -> None:
    def boom(url: str, payload: dict[str, Any]) -> Iterator[str]:
        raise OSError("refused")

    events = list(cli.stream_turn("http://x", "hi", open_stream=boom))
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert "couldn't reach" in events[0]["reply"].lower()


def test_main_no_args_launches_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "ensure_daemon", lambda base, port, **_k: None)
    import autobot.cli.tui as tui

    monkeypatch.setattr(tui, "run", lambda base_url, cwd: launched.append((base_url, cwd)))
    rc = cli.main(["--port", "8766"])
    assert rc == 0 and launched and launched[0][0].endswith("8766")


def test_main_no_args_without_textual_prints_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "ensure_daemon", lambda base, port, **_k: None)

    def raise_import(base_url: str, cwd: str) -> None:
        raise ImportError("No module named 'textual'")

    import autobot.cli.tui as tui

    monkeypatch.setattr(tui, "run", raise_import)
    rc = cli.main([])
    assert rc == 1 and "tui" in capsys.readouterr().err.lower()


def test_main_with_text_still_one_shot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "ensure_daemon", lambda base, port, **_k: None)
    monkeypatch.setattr(cli, "run_coder_turn", lambda base, text, **k: "the reply")
    rc = cli.main(["-p", "do a thing"])
    assert rc == 0 and "the reply" in capsys.readouterr().out
