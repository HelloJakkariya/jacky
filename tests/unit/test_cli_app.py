"""The prompt_toolkit Application, driven headlessly via a pipe input + DummyOutput."""

from __future__ import annotations

import asyncio
from io import StringIO
from typing import Any

from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from autobot.cli.app import AppSurface, JackApp, parse_gate_answer
from autobot.cli.classify import Segment
from autobot.cli.prompt import Answer


def _run_with_feed(feed_body: Any, *, cwd: str = "/x") -> JackApp:
    """Drive a JackApp with a concurrent feeder coroutine ``feed_body(inp, japp)``; return app."""
    holder: dict[str, JackApp] = {}

    async def noop(_t: str, _n: int) -> None:
        return None

    async def _drive() -> None:
        with create_pipe_input() as inp:
            japp = JackApp(cwd=cwd, run_turn=noop, commands={}, input=inp, output=DummyOutput())
            holder["app"] = japp

            async def feed() -> None:
                await feed_body(inp, japp)
                inp.send_text("\x04")  # quit

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()

    asyncio.run(_drive())
    return holder["app"]


def _bracketed(text: str) -> str:
    """Wrap ``text`` in the terminal bracketed-paste escape sequence."""
    return f"\x1b[200~{text}\x1b[201~"


def test_bracketed_paste_large_text_collapses_and_expands() -> None:
    big = "\n".join(f"line {i}" for i in range(14))  # > MAX_INPUT_LINES → collapses

    async def feed(inp: Any, japp: JackApp) -> None:
        inp.send_text(_bracketed(big))
        await asyncio.sleep(0.15)

    japp = _run_with_feed(feed)
    assert "[Pasted #1 · 14 lines]" in japp._input.text  # collapsed in the input
    assert japp.expand_pastes(japp._input.text) == big  # expands back to the real content on send


def test_bracketed_paste_small_multiline_shows_inline() -> None:
    small = "line a\nline b\nline c"  # fits in the growing box → not collapsed

    async def feed(inp: Any, japp: JackApp) -> None:
        inp.send_text(_bracketed(small))
        await asyncio.sleep(0.15)

    japp = _run_with_feed(feed)
    assert japp._input.text == small  # shown inline (box grows), no placeholder
    assert "[Pasted" not in japp._input.text


def test_backspace_removes_the_whole_pasted_block() -> None:
    big = "\n".join(f"row {i}" for i in range(14))

    async def feed(inp: Any, japp: JackApp) -> None:
        inp.send_text(_bracketed(big))
        await asyncio.sleep(0.1)
        inp.send_text("\x7f")  # Backspace over the placeholder
        await asyncio.sleep(0.1)

    japp = _run_with_feed(feed)
    assert "[Pasted" not in japp._input.text  # the whole placeholder was removed, not one char


def test_up_arrow_recalls_previous_submissions() -> None:
    captured: dict[str, str] = {}

    async def noop(_t: str, _n: int) -> None:
        return None

    async def _drive() -> None:
        with create_pipe_input() as inp:
            japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=inp, output=DummyOutput())

            async def feed() -> None:
                inp.send_text("first message\r")
                await asyncio.sleep(0.1)
                inp.send_text("second message\r")
                await asyncio.sleep(0.1)
                inp.send_text("\x1b[A")  # ↑ recalls the most recent submission
                await asyncio.sleep(0.1)
                captured["up"] = japp._input.text
                inp.send_text("\x04")

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()

    asyncio.run(_drive())
    assert captured["up"] == "second message"


def test_bracketed_paste_existing_path_becomes_a_mention(tmp_path: Any) -> None:
    from pathlib import Path

    f = Path(tmp_path) / "notes.md"
    f.write_text("hi", encoding="utf-8")

    async def feed(inp: Any, japp: JackApp) -> None:
        inp.send_text(_bracketed(str(f)))
        await asyncio.sleep(0.15)

    japp = _run_with_feed(feed, cwd=str(tmp_path))
    assert japp._input.text.strip() == f"@{f}"  # a pasted path becomes an @mention


def test_command_output_carded_then_expanded() -> None:
    async def noop(_t: str, _n: int) -> None:
        return None

    japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput())
    surface = AppSurface(japp)
    surface.commit_command("$ npm test", ["PASS a", "PASS b", "PASS c"], gated=False)
    japp._transcript_text()  # force a compose
    assert "3 lines" in japp._transcript and "^O to view" in japp._transcript  # compact card
    assert "PASS a" not in japp._transcript  # full output NOT in the card
    assert japp.expand_output() is True  # ^O / /output expands it in place
    assert "Command result" in japp._transcript  # simple header (not the full command again)
    assert "PASS a" in japp._transcript and "PASS c" in japp._transcript


def test_ctrl_o_toggles_expand_and_collapse() -> None:
    async def noop(_t: str, _n: int) -> None:
        return None

    japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput())
    AppSurface(japp).commit_command("$ ls", ["a", "b"], gated=False)
    assert japp.expand_output() is True  # first ^O expands
    assert "a" in japp._transcript and "b" in japp._transcript
    assert japp.expand_output() is True  # ^O again collapses (a toggle, not a no-op)
    assert "Command result" not in japp._transcript  # back to the compact card
    assert "2 lines · ^O to view" in japp._transcript


def test_growing_transcript_stays_scrollable_when_scrolled_up() -> None:
    # The regression: while a turn streams output, the chat grows; if the user scrolled up to
    # read, new output must NOT yank them back to the bottom. Following resumes only when they
    # scroll back down (or submit a new turn).
    from rich.text import Text

    async def noop(_t: str, _n: int) -> None:
        return None

    japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput())
    for i in range(20):
        japp.append_transcript(Text(f"line {i}"))
    assert japp._follow is True  # fresh output tails to the bottom
    assert japp._cursor_y() == japp._total_lines()

    japp._scroll_by(-5)  # user scrolls up to read
    assert japp._follow is False
    parked = japp._cursor_y()

    for i in range(20, 40):  # lots more output arrives while parked
        japp.append_transcript(Text(f"line {i}"))
    assert japp._follow is False  # still detached — not yanked to the bottom
    assert japp._cursor_y() == parked  # the view stayed exactly where the user left it

    japp._scroll_by(10_000)  # scrolling back past the bottom resumes tailing
    assert japp._follow is True
    assert japp._cursor_y() == japp._total_lines()


def test_tab_descends_into_a_folder(tmp_path: Any) -> None:
    (tmp_path / "src" / "cli").mkdir(parents=True)
    (tmp_path / "src" / "app.py").write_text("")

    async def feed(inp: Any, japp: JackApp) -> None:
        inp.send_text("@s")  # completes to the only match, the "src" folder
        await asyncio.sleep(0.1)
        inp.send_text("\t")  # Tab accepts + descends → "@src/"
        await asyncio.sleep(0.1)

    japp = _run_with_feed(feed, cwd=str(tmp_path))
    assert japp._input.text == "@src/"  # folder accepted with a trailing slash (still descending)


def test_tab_selects_a_file(tmp_path: Any) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("")

    async def feed(inp: Any, japp: JackApp) -> None:
        inp.send_text("@src/app")
        await asyncio.sleep(0.1)
        inp.send_text("\t")  # Tab selects the file (no trailing slash, no further descent)
        await asyncio.sleep(0.1)

    japp = _run_with_feed(feed, cwd=str(tmp_path))
    assert japp._input.text == "@src/app.py "  # file selected + a trailing space ends the mention


def test_enter_includes_a_folder_without_descending(tmp_path: Any) -> None:
    (tmp_path / "src" / "cli").mkdir(parents=True)

    async def feed(inp: Any, japp: JackApp) -> None:
        inp.send_text("@s")
        await asyncio.sleep(0.1)
        inp.send_text("\x1b[B")  # ↓ highlights the first match (the "src" folder)
        await asyncio.sleep(0.1)
        inp.send_text("\r")  # Enter INCLUDES the folder as-is (does not descend into it)
        await asyncio.sleep(0.1)

    japp = _run_with_feed(feed, cwd=str(tmp_path))
    assert japp._input.text == "@src/ "  # folder included (trailing space), not descended


def test_auto_command_card_colors_the_command_white_for_safety() -> None:
    from autobot.cli.app import _CommandBlock

    safe = "224;230;226"  # the near-white "safe" (#e0e6e2) truecolor code
    auto = _CommandBlock("$ rm -rf build", ["x"], gated=False).render(80)
    assert "$ rm -rf build" in auto and safe in auto  # auto-run command is white, not dim gray
    gated = _CommandBlock("$ rm -rf build", ["x"], gated=True).render(80)
    assert "$ rm -rf build" not in gated  # gated card is result-only (gate / red line shows it)


def test_transcript_separates_blocks_with_a_blank_line() -> None:
    import re

    from rich.text import Text

    async def noop(_t: str, _n: int) -> None:
        return None

    japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput())
    japp.append_transcript(Text("first block"))
    japp.append_transcript(Text("second block"))
    plain = re.sub(r"\x1b\[[0-9;]*m", "", japp._transcript)
    lines = [ln.strip() for ln in plain.split("\n")]
    i = lines.index("first block")
    j = lines.index("second block")
    assert j == i + 2  # exactly one blank line between the two blocks
    assert lines[i + 1] == ""


def test_expand_output_when_nothing_stored_is_false() -> None:
    async def noop(_t: str, _n: int) -> None:
        return None

    japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput())
    assert japp.expand_output() is False


def test_parse_gate_answer_plan_and_permission() -> None:
    plan = Segment("plan", "the plan")
    assert parse_gate_answer(plan, "y") == Answer("approve")
    assert parse_gate_answer(plan, "n") == Answer("reject")
    assert parse_gate_answer(plan, "") == Answer("reject")
    assert parse_gate_answer(plan, "use a retry loop") == Answer("refine", "use a retry loop")
    pending = Segment("pending", "run pytest?")
    assert parse_gate_answer(pending, "yes") == Answer("yes")
    assert parse_gate_answer(pending, "nope") == Answer("no")


def test_submitting_a_line_spawns_a_turn() -> None:
    spawned: list[tuple[str, int]] = []

    async def fake_run_turn(text: str, turn_no: int) -> None:
        spawned.append((text, turn_no))

    async def _drive() -> None:
        with create_pipe_input() as inp:
            japp = JackApp(
                cwd="/x", run_turn=fake_run_turn, commands={}, input=inp, output=DummyOutput()
            )

            async def feed() -> None:
                inp.send_text("hello\r")  # submit one turn
                await asyncio.sleep(0.1)  # let the turn task run before we quit
                inp.send_text("\x04")  # Ctrl-D → EOF → exit

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()

    asyncio.run(_drive())
    assert spawned == [("hello", 0)]


def test_escape_cancels_a_running_turn() -> None:
    cancelled = asyncio.Event()

    async def slow_run_turn(text: str, turn_no: int) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _drive() -> None:
        with create_pipe_input() as inp:
            japp = JackApp(
                cwd="/x", run_turn=slow_run_turn, commands={}, input=inp, output=DummyOutput()
            )

            async def feed() -> None:
                inp.send_text("do it\r")  # start a slow turn
                await asyncio.sleep(0.2)  # let the task actually start
                inp.send_text("\x1b")  # ESC → interrupt
                for _ in range(60):  # wait (≤3s) for ESC to actually cancel the turn
                    if cancelled.is_set():
                        break
                    await asyncio.sleep(0.05)
                inp.send_text("\x04")  # Ctrl-D → exit (only after ESC has cancelled)

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()

    asyncio.run(_drive())
    assert cancelled.is_set()  # ESC — not shutdown teardown — cancelled the turn


def test_appsurface_set_activity_updates_fragments() -> None:
    async def noop(_t: str, _n: int) -> None:
        return None

    japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput())
    surface = AppSurface(japp, Console(file=StringIO(), force_terminal=False))
    surface.set_activity("Reading parser.py")
    assert japp._activity == "Reading parser.py"
    surface.clear_activity()
    assert japp._activity is None


def test_escape_refills_the_input_with_the_interrupted_text() -> None:
    async def slow_run_turn(text: str, turn_no: int) -> None:
        await asyncio.sleep(10)

    japp: JackApp

    async def _drive() -> str:
        nonlocal japp
        with create_pipe_input() as inp:
            japp = JackApp(
                cwd="/x", run_turn=slow_run_turn, commands={}, input=inp, output=DummyOutput()
            )

            async def feed() -> None:
                inp.send_text("fix the parser bug\r")  # start a turn (input cleared)
                await asyncio.sleep(0.2)
                inp.send_text("\x1b")  # ESC → cancel + refill input
                await asyncio.sleep(0.2)
                inp.send_text("\x04")  # quit

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()
        return japp._input.text

    assert asyncio.run(_drive()) == "fix the parser bug"  # refilled for editing/resending


def test_begin_modal_resolves_from_single_keypress() -> None:
    got: list[Answer] = []

    async def run_turn(text: str, turn_no: int) -> None:
        got.append(await japp.begin_modal(Segment("plan", "the plan")))

    japp: JackApp

    async def _drive() -> None:
        nonlocal japp
        with create_pipe_input() as inp:
            japp = JackApp(
                cwd="/x", run_turn=run_turn, commands={}, input=inp, output=DummyOutput()
            )

            async def feed() -> None:
                inp.send_text("do it\r")  # spawn a turn that opens a gate
                await asyncio.sleep(0.15)
                inp.send_text("y")  # single keypress resolves it — NO Enter needed
                await asyncio.sleep(0.15)
                inp.send_text("\x04")  # exit

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()

    asyncio.run(_drive())
    assert got == [Answer("approve")]


def test_on_task_finished_when_idle_runs_a_continuation_turn() -> None:
    seen: list[str] = []

    async def fake_run_turn(text: str, turn_no: int) -> None:
        seen.append(text)

    async def _drive() -> None:
        with create_pipe_input() as inp:
            japp = JackApp(
                cwd="/x", run_turn=fake_run_turn, commands={}, input=inp, output=DummyOutput()
            )

            async def fire() -> None:
                await asyncio.sleep(0.05)
                japp.on_task_finished([{"id": "task-3", "status": "done"}])
                await asyncio.sleep(0.05)
                inp.send_text("\x04")  # exit

            asyncio.get_running_loop().create_task(fire())
            await japp.run_async()

    asyncio.run(_drive())
    assert seen and "background task" in seen[0].lower()


def test_input_kind_modal_resolves_free_text() -> None:
    import asyncio

    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput

    from autobot.cli.app import JackApp
    from autobot.cli.classify import Segment

    async def noop(text: str, turn_no: int) -> None:
        return None

    japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput())

    async def scenario() -> None:
        task = asyncio.ensure_future(japp.begin_modal(Segment("input", "Server id?")))
        await asyncio.sleep(0)
        assert japp._modal_edit is True  # typing goes to the buffer, not y/n keys
        assert japp._secret_mode is False
        japp._input.text = "postgres"
        japp._on_accept(japp._input)
        ans = await task
        assert ans.value == "refine" and ans.text == "postgres"

    asyncio.run(scenario())


def test_secret_kind_masks_and_skips_history() -> None:
    import asyncio

    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput

    from autobot.cli.app import JackApp
    from autobot.cli.classify import Segment

    async def noop(text: str, turn_no: int) -> None:
        return None

    japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput())

    async def scenario() -> None:
        task = asyncio.ensure_future(japp.begin_modal(Segment("secret", "Token?")))
        await asyncio.sleep(0)
        assert japp._secret_mode is True
        japp._input.text = "sk-verysecret"
        # Drive the real prompt_toolkit accept path (not _on_accept directly): this
        # exercises validate_and_handle()'s unconditional append_to_history() call too,
        # so the assertion below checks the real history object, not a comment's claim.
        japp._input.validate_and_handle()
        ans = await task
        assert ans.text == "sk-verysecret"
        assert japp._input.text == ""  # buffer wiped by the handler itself
        assert japp._secret_mode is False
        # The guarantee holds because the handler wipes the buffer (buff.reset())
        # before append_to_history() runs — append_to_history() records only
        # non-empty text, so it sees "" and skips the secret entirely.
        assert "sk-verysecret" not in japp._input.history.get_strings()

    asyncio.run(scenario())


def _hud_app() -> Any:
    async def noop(_t: str, _n: int) -> None:
        return None

    return JackApp(cwd="/x", run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput())


def test_status_text_renders_hud_segments() -> None:
    japp = _hud_app()
    japp.hud_state.autonomy = "auto"
    japp.hud_state.model = "opus"
    japp.hud_state.used = 38000
    japp.hud_state.window = 200000
    japp.hud_state.branch = "main"
    japp.hud_state.cwd = "~/x"
    text = "".join(t for _s, t in japp._status_text())
    assert "auto" in text
    assert "opus" in text
    assert "ctx 19%" in text
    assert "main" in text


def test_on_context_event_updates_state() -> None:
    japp = _hud_app()
    japp.on_context_event({"type": "context", "used": 50000, "window": 200000, "model": "opus"})
    assert japp.hud_state.used == 50000
    assert japp.hud_state.window == 200000
    assert japp.hud_state.model == "opus"


def test_skills_and_ghost_are_wired_into_the_live_input() -> None:
    """``skills=`` threads into the live buffer's completer + inline ghost (wiring integration).

    Proves the app actually composes the completer with the passed-in skills and installs a
    :class:`JackAutoSuggest`, so the behaviours unit-tested on the pure pieces are what the
    running input uses. No event loop needed — reach into the constructed buffer directly.
    """
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from autobot.cli.prompt import JackAutoSuggest

    async def noop(_t: str, _n: int) -> None:
        return None

    japp = JackApp(
        cwd="/x",
        run_turn=noop,
        commands={"/exit": "quit", "/help": "help"},
        skills=[("deep-research", "Fan-out research"), ("explain-code", "Walk code")],
        input=DummyInput(),
        output=DummyOutput(),
    )
    comp = japp._input.completer
    assert comp is not None
    ev = CompleteEvent()
    start = [c.text for c in comp.get_completions(Document("/", 1), ev)]
    assert "/help" in start and "/deep-research" in start  # line start: commands + skills
    mid = [c.text for c in comp.get_completions(Document("fix /e", 6), ev)]
    assert "/explain-code" in mid and "/exit" not in mid  # mid-line: skills only, never /exit

    assert isinstance(japp._input.auto_suggest, JackAutoSuggest)
    sug = japp._input.auto_suggest.get_suggestion(japp._input, Document("/he", 3))
    assert sug is not None and sug.text == "lp"  # inline ghost previews the top match
