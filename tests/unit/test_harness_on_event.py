"""The harness emits tool start/end events via on_event and tool_label maps calls."""

from __future__ import annotations

from typing import Any

from autobot.agent.chat_model import ChatResponse
from autobot.agent.harness import AgentHarness, tool_label
from autobot.agent.session_store import SessionStore
from autobot.core.types import ToolCall, ToolResult


class _FakeModel:
    """A ChatModel stub: one round with a tool call, then a final reply."""

    def __init__(self) -> None:
        self._round = 0

    def begin_turn(self, session: Any, user_text: str) -> None: ...
    def record_results(self, session: Any, results: list[tuple[ToolCall, ToolResult]]) -> None: ...
    def handle_discovery(self, session: Any, call: ToolCall) -> str | None:
        return None

    def finalize_turn(self, session: Any) -> list[dict[str, Any]]:
        return []

    def final_answer_no_tools(self, session: Any) -> str:
        return ""

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        return ""

    def send(self, session: Any, on_event: Any = None) -> ChatResponse:
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                text="", tool_calls=[ToolCall(name="read_file", arguments={"path": "api.py"})]
            )
        return ChatResponse(text="done", tool_calls=[])


def test_tool_label_maps_common_calls() -> None:
    assert tool_label(ToolCall(name="read_file", arguments={"path": "api.py"})) == "Read api.py"
    assert "Searched" in tool_label(ToolCall(name="grep", arguments={"pattern": "fetch"}))
    assert tool_label(ToolCall(name="run_command", arguments={"command": "pytest -q"})).startswith(
        "$ "
    )
    # Unknown tools fall back to a humanized name (consistent casing with the labels above).
    assert tool_label(ToolCall(name="mystery", arguments={})) == "Mystery"
    assert tool_label(ToolCall(name="repo_map", arguments={})) == "Repo map"


def test_tool_label_names_the_activated_skill() -> None:
    # Activating a skill must show WHICH skill (the reported bug was a bare "Skill").
    assert tool_label(ToolCall(name="skill", arguments={"name": "spinup"})) == "Skill: spinup"
    read = tool_label(ToolCall(name="read_skill_file", arguments={"name": "spinup", "path": "x"}))
    assert read == "Skill file spinup/x"


def test_tool_label_shows_just_the_file_name() -> None:
    # Paths collapse to the bare file name — a long absolute or nested path is hard to scan.
    assert tool_label(ToolCall("read_file", {"path": "/work/proj/package.json"})) == (
        "Read package.json"
    )
    assert tool_label(ToolCall("read_file", {"path": "src/support/app.py"})) == "Read app.py"
    assert tool_label(ToolCall("edit_file", {"path": "/a/b/tests/data-integrity.js"})) == (
        "Edited data-integrity.js"
    )


def test_run_turn_emits_tool_start_and_end(tmp_path: Any) -> None:
    from pathlib import Path

    store = SessionStore(str(Path(tmp_path) / "sessions"))
    harness = AgentHarness(_FakeModel(), store, cwd=".", model_name="fake")
    events: list[dict[str, Any]] = []

    def execute(call: ToolCall) -> ToolResult:
        return ToolResult(name=call.name, content="ok", ok=True)

    reply = harness.run_turn("do it", execute, on_event=events.append)
    assert reply == "done"
    kinds = [(e.get("event"), e.get("name")) for e in events if e.get("type") == "tool"]
    assert ("start", "read_file") in kinds
    assert ("end", "read_file") in kinds


def test_run_turn_without_on_event_is_unchanged(tmp_path: Any) -> None:
    from pathlib import Path

    store = SessionStore(str(Path(tmp_path) / "sessions"))
    harness = AgentHarness(_FakeModel(), store, cwd=".", model_name="fake")
    reply = harness.run_turn("do it", lambda c: ToolResult(name=c.name, content="ok", ok=True))
    assert reply == "done"  # no on_event: same result, no error


def test_output_sink_emits_output_events_during_execute(tmp_path: Any) -> None:
    from pathlib import Path

    from autobot.core.streaming import output_sink

    store = SessionStore(str(Path(tmp_path) / "sessions"))
    harness = AgentHarness(_FakeModel(), store, cwd=".", model_name="fake")
    events: list[dict[str, Any]] = []

    def execute(call: ToolCall) -> ToolResult:
        sink = output_sink.get()  # the harness sets this for the duration of the call
        assert sink is not None
        sink("first line")
        sink("second line")
        return ToolResult(name=call.name, content="ok", ok=True)

    harness.run_turn("do it", execute, on_event=events.append)

    outputs = [e for e in events if e.get("type") == "output"]
    assert [e["text"] for e in outputs] == ["first line", "second line"]
    assert all(e["name"] == "read_file" for e in outputs)  # bound to the executing tool
    assert output_sink.get() is None  # reset after the turn


class _FlailingModel:
    """Issues a NEW distinct tool call every round (never a repeat) that always fails."""

    def __init__(self) -> None:
        self.rounds = 0

    def begin_turn(self, session: Any, user_text: str) -> None: ...
    def record_results(self, session: Any, results: list[tuple[ToolCall, ToolResult]]) -> None: ...
    def handle_discovery(self, session: Any, call: ToolCall) -> str | None:
        return None

    def finalize_turn(self, session: Any) -> list[dict[str, Any]]:
        return []

    def final_answer_no_tools(self, session: Any) -> str:
        return "forced final answer"

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        return ""

    def send(self, session: Any, on_event: Any = None) -> ChatResponse:
        self.rounds += 1
        # A fresh, distinct command each round -> not a doom-repeat and not all-repeat, but
        # it never succeeds, so the diminishing-returns guard is what must stop the turn.
        return ChatResponse(
            text="",
            tool_calls=[ToolCall(name="run_command", arguments={"command": f"try-{self.rounds}"})],
        )


def test_run_turn_stops_after_unproductive_rounds(tmp_path: Any) -> None:
    from pathlib import Path

    store = SessionStore(str(Path(tmp_path) / "sessions"))
    model = _FlailingModel()
    # A high backstop so it's the diminishing-returns guard (not max_rounds) that stops us.
    harness = AgentHarness(
        model, store, cwd=".", model_name="fake", max_rounds=50, max_unproductive=3
    )

    def execute(call: ToolCall) -> ToolResult:
        return ToolResult(name=call.name, content="boom", ok=False)  # always fails

    reply = harness.run_turn("do it", execute)
    assert model.rounds == 3  # stopped at max_unproductive, NOT at max_rounds (50)
    assert "boom" in reply  # surfaces the last failure, not a generic "step limit"
