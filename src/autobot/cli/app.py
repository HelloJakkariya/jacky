"""The one full-screen prompt_toolkit Application for the coding-agent CLI.

Full-screen so the input + status bar are **docked at the bottom** by construction, with a
scrollable transcript above and a transient live region (spinner / current tool / gate
affordance) between them. Finished lines are rendered (rich → ANSI) into the transcript
buffer — no ``run_in_terminal`` (which is CPR-dependent and renders the input mid-screen in
inline mode). The turn runs as a cancellable asyncio task, so the input stays pinned mid-turn
and ``escape`` detaches the client (cancels the local drive; the daemon has no abort endpoint
yet — a tracked follow-up).

Plan/permission gates are answered through the *same* pinned input (type ``y``/``n`` or, for a
plan, type the change you want) — a future handshake between the turn task (awaiting the
answer) and the accept handler (resolving it). No nested Application, no dynamic key layers.

Tradeoff vs. the old inline shell: the transcript scrolls *within* the app (PgUp/PgDn/mouse),
not the terminal's native scrollback — the cost of a truly pinned bottom.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from io import StringIO
from typing import TYPE_CHECKING, Any

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.bindings.auto_suggest import load_auto_suggest_bindings
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import ConditionalProcessor, PasswordProcessor
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

from autobot.cli import live_region, paste, theme
from autobot.cli.paste import PasteStore
from autobot.cli.prompt import Answer, JackAutoSuggest, JackCompleter
from autobot.cli.theme import jack_theme
from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from autobot.cli.classify import Segment

_log = get_logger("cli")

_RunTurn = Callable[[str, int], Awaitable[None]]
_TICK_INTERVAL_S = 0.12  # live-region repaint cadence (spinner frame + elapsed seconds)
_DOUBLE_ESC_S = 0.6  # a second Esc within this window clears the input
_WHEEL_STEP = 6  # lines scrolled per mouse-wheel notch (higher = snappier trackpad scrolling)
MAX_INPUT_LINES = 10  # the input box grows up to this many lines, then scrolls within
MAX_EXPAND_LINES = 300  # cap when expanding a command's output (^O / /output) — last N lines

_STYLE = Style.from_dict(
    {
        "spinner": "#4fd6b8",
        "verb": "bold",
        "teal": "#4fd6b8",
        "dim": "#5b665f",
        "prompt": "#4fd6b8 bold",
        "amber": "#e6b25f",
        "green": "#83c878",  # HUD context bar: healthy (bg inherited from the status window)
        "red": "#e2726f",  # HUD context bar: near-full
        "status": "#c7d0cb bg:#1a231f",
        "status.key": "#4fd6b8 bg:#1a231f",
        "inputframe": "#4fd6b8",  # teal border around the input box
        "auto-suggestion": "#5b665f",  # dim grey ghost text for the inline autocomplete
    }
)

_CONTINUATION_PROMPT = (
    "A background task you started has finished (its result is included above). Continue the "
    "task using that result; if everything is now complete, briefly confirm what happened."
)


def render_ansi(renderable: Any, width: int) -> str:
    """Render a rich renderable (or str) to an ANSI string for the transcript.

    A single-column left/right margin keeps transcript content off the terminal edge without
    over-indenting; every committed block gets the same margin so the gutter glyphs line up.
    """
    buf = StringIO()
    from rich.console import Console
    from rich.padding import Padding

    Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        theme=jack_theme(),
        width=max(20, width),
        soft_wrap=False,
    ).print(Padding(renderable, (0, 1, 0, 1)))  # (top, right, bottom, left) — a 1-col margin
    return buf.getvalue()


def parse_gate_answer(seg: Segment, line: str) -> Answer:
    """Map a typed gate reply to an :class:`Answer` (pure — unit-tested).

    Plan gate: ``y``/``yes`` approves, ``n``/``no``/empty rejects, any other text is taken as
    a refinement. Permission gate: ``y``/``yes`` allows, anything else declines.
    """
    text = line.strip()
    low = text.lower()
    if seg.kind == "plan":
        if low in ("y", "yes"):
            return Answer("approve")
        if low in ("n", "no", ""):
            return Answer("reject")
        return Answer("refine", text)
    return Answer("yes") if low in ("y", "yes") else Answer("no")


class _Block:
    """One transcript block; renders to an ANSI string at a given width.

    The transcript is a list of these (not a flat string) so ^O can flip every command block
    between its compact card and full output *in place* and recompose, keeping the view stable.
    """

    def render(self, width: int) -> str:
        """Return this block's ANSI text at ``width``."""
        raise NotImplementedError


@dataclass
class _PlainBlock(_Block):
    """A finished renderable, pre-rendered to ANSI at commit-time width (never reflows)."""

    ansi: str

    def render(self, width: int) -> str:
        """Return the pre-rendered ANSI (width is ignored — it was fixed at commit time)."""
        return self.ansi


@dataclass
class _CommandBlock(_Block):
    """A finished command: a compact card when collapsed, full output when expanded (^O).

    Renders are cached per ``(width, expanded)`` so recomposing the transcript on every append
    stays cheap (a command re-renders only when the width or its expand state changes).
    """

    label: str
    output: list[str]
    gated: bool
    expanded: bool = False
    _cache: dict[tuple[int, bool], str] = field(default_factory=dict, repr=False, compare=False)

    def render(self, width: int) -> str:
        """Return the card (collapsed) or the full output block (expanded), cached per key."""
        key = (width, self.expanded)
        cached = self._cache.get(key)
        if cached is None:
            cached = render_ansi(self._renderable(), width)
            self._cache[key] = cached
        return cached

    def _renderable(self) -> Any:
        from rich.console import Group
        from rich.text import Text

        n = len(self.output)
        if not self.expanded:
            plural = "line" if n == 1 else "lines"
            summary = f"{n} {plural} · ^O to view" if n else "done"
            card = Text(theme.NEST_INDENT)
            if not self.gated:
                # Auto mode ran the command WITHOUT a gate — show it in white ("safe"/allowed)
                # so it's clearly legible in the dim activity stream, not lost as gray. Gated
                # commands were shown (amber) by the gate; a rejected one is shown red there.
                card.append(self.label, style="safe")
                card.append(" · ", style="tool")
            card.append(summary, style="tool")
            return card
        shown = self.output[-MAX_EXPAND_LINES:]
        header = f"{theme.NEST_INDENT}Command result"  # simple label — not the full command again
        if n > len(shown):
            header += f"  (last {len(shown)} of {n} lines)"
        body = Text("\n".join(shown) or "(no output)", style="tool")
        # The composer separates this block from its neighbours with blank lines already.
        return Group(Text(header, style="tool"), body)


class _TranscriptControl(FormattedTextControl):
    """A transcript control that forwards mouse-wheel scroll to a callback (signed line delta)."""

    def __init__(self, *args: Any, on_scroll: Callable[[int], None], **kwargs: Any) -> None:
        """Wrap ``FormattedTextControl`` with an ``on_scroll(delta_lines)`` wheel callback."""
        super().__init__(*args, **kwargs)
        self._on_scroll = on_scroll

    def mouse_handler(self, mouse_event: MouseEvent) -> Any:
        """Route wheel up/down to the scroll callback; defer everything else to the base.

        On a Mac laptop PageUp/PageDown mean Fn+↑/↓ — awkward — so the trackpad/wheel is the
        natural way to scroll. With mouse tracking on, the terminal sends wheel events here
        (instead of translating a trackpad scroll into arrow keys the input would swallow).
        """
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._on_scroll(-_WHEEL_STEP)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._on_scroll(_WHEEL_STEP)
            return None
        return super().mouse_handler(mouse_event)


class JackApp:
    """A full-screen app: scrollable transcript, live region, docked input + status bar."""

    def __init__(
        self,
        *,
        cwd: str,
        run_turn: _RunTurn,
        commands: dict[str, str],
        skills: Sequence[tuple[str, str]] = (),
        context: dict[str, str] | None = None,
        intro: Any | None = None,
        on_interrupt: Callable[[], Any] | None = None,
        history: History | None = None,
        input: Any | None = None,
        output: Any | None = None,
    ) -> None:
        """Wire the app; ``run_turn(text, turn_no)`` is the injected turn coroutine.

        ``commands``/``skills`` populate the ``/`` completer + inline ghost text (skills as
        ``(name, description)``). ``context`` populates the status bar (model/autonomy/branch);
        ``intro`` seeds the transcript (the welcome banner). ``input``/``output`` are forwarded
        to the ``Application`` (tests inject a pipe input + ``DummyOutput``).
        """
        self._cwd = cwd
        self._run_turn = run_turn
        self._commands = commands
        self._context = context or {}
        # HUD: resolve the segment rows once + seed the live state from the startup context.
        from autobot.cli import hud
        from autobot.config import Settings

        hud_settings = Settings.load()
        self._hud = hud
        self._hud_rows = hud.resolve_config(hud_settings)
        self._hud_sep = hud_settings.hud_separator
        self.hud_state = hud.HudState(
            autonomy=self._context.get("autonomy", ""),
            model=self._context.get("model", ""),
            provider=self._context.get("provider", ""),
            cwd=self._context.get("cwd", ""),
            branch=self._context.get("branch", ""),
        )
        self._intro = intro
        self._on_interrupt = on_interrupt
        self._seeded = False
        self._turn_no = 0
        self._exiting = False
        self._last_turn_text = ""  # restored into the input when a turn is esc-interrupted
        self._last_esc = 0.0  # monotonic time of the last Esc (for double-Esc = clear input)
        self._task: asyncio.Task[None] | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._blocks: list[_Block] = []  # transcript blocks (plain + collapsible command cards)
        self._block_starts: list[int] = []  # each block's start line in the composed transcript
        self._transcript = ""  # composed ANSI (rebuilt from _blocks); shown in the scroll region
        self._follow = True  # auto-tail the transcript to the bottom (PgUp detaches)
        self._scroll_pos = 0  # manual scroll offset when not following
        # Live-region state (painted by _live_content on the app loop — single owner):
        # _activity is the current-action label shown by the spinner (None hides the region);
        # _todos is the model's live checklist, shown as a windowed panel under the spinner.
        self._activity: str | None = None
        self._todos: list[tuple[str, str]] = []
        self._modal_hint: list[tuple[str, str]] | None = None
        self._turn_started = 0.0
        self._frame_i = 0
        self._modal: asyncio.Future[Answer] | None = None
        self._modal_seg: Segment | None = None
        self._modal_edit = False  # True while typing a plan refinement (after pressing 'e')
        self._secret_mode = False  # mask the input buffer while a "secret" ask is active
        self._pending_pickups: list[dict[str, Any]] = []
        self._pastes = PasteStore()  # large pastes stashed behind [Pasted #N] placeholders
        completer = JackCompleter(commands, cwd, skills=skills)
        self._input = Buffer(
            accept_handler=self._on_accept,
            completer=completer,
            auto_suggest=JackAutoSuggest(completer),  # dim inline ghost of the top match
            complete_while_typing=True,
            history=history or InMemoryHistory(),  # ↑/↓ recall previous submissions
            multiline=True,  # box grows for multi-line paste/typing (Enter submits; ^J newline)
        )
        self._transcript_window = Window(
            _TranscriptControl(
                self._transcript_text,
                focusable=False,
                show_cursor=False,
                # A hidden cursor on the last line makes the Window auto-scroll to the bottom
                # (tail). Wheel / PgUp move the cursor up to scroll back; new output re-tails it.
                get_cursor_position=lambda: Point(x=0, y=self._cursor_y()),
                on_scroll=self._scroll_by,
            ),
            wrap_lines=True,
        )
        self.app: Application[None] = Application(
            layout=self._build_layout(),
            key_bindings=merge_key_bindings(
                # auto-suggest bindings: →/End/^E/^F accept the inline ghost (only when one is
                # shown and the cursor is at the end — they fall through to normal cursor moves
                # otherwise), so they never clash with the custom bindings below.
                [load_key_bindings(), load_auto_suggest_bindings(), self._bindings()]
            ),
            style=_STYLE,
            full_screen=True,  # docks input + status bar at the bottom, transcript above
            mouse_support=True,  # capture wheel → scroll the transcript (Mac trackpad-friendly)
            input=input,
            output=output,
        )

    @property
    def busy(self) -> bool:
        """True while a turn task is running."""
        return self._task is not None and not self._task.done()

    @property
    def turn_no(self) -> int:
        """How many turns have been started this session."""
        return self._turn_no

    def _cols(self) -> int:
        try:
            return self.app.output.get_size().columns or 100
        except Exception:
            return 100

    def _total_lines(self) -> int:
        """Number of logical lines in the transcript (0-based index of the last line)."""
        return self._transcript.count("\n")

    def _cursor_y(self) -> int:
        """The transcript cursor line: the last line (tail) while following, else the scroll pos.

        The Window scrolls to keep this line visible, so pointing it at the last line tails the
        transcript to the bottom; PgUp/wheel park it earlier to scroll back.
        """
        return self._total_lines() if self._follow else min(self._scroll_pos, self._total_lines())

    def _scroll_by(self, delta: int) -> None:
        """Move the transcript view by ``delta`` lines (negative = up); driven by PgUp/PgDn.

        Scrolling to/past the bottom resumes auto-tailing; scrolling up detaches from the tail
        so a growing transcript stays put where the user parked it.
        """
        target = self._cursor_y() + delta
        if target >= self._total_lines():
            self._follow = True
        else:
            self._follow = False
            self._scroll_pos = max(0, target)
        self.app.invalidate()

    def _transcript_text(self) -> ANSI:
        """The transcript region's content (seed the intro lazily once width is known)."""
        if not self._seeded and self._intro is not None:
            self._blocks.insert(0, _PlainBlock(render_ansi(self._intro, self._cols())))
            self._recompose()
            self._seeded = True
        return ANSI(self._transcript)

    def _recompose(self) -> None:
        """Rebuild the composed transcript + per-block start lines, one blank line between blocks.

        The composer owns spacing: every block (user turn, each activity line, the reply, a
        command card) is separated from its neighbours by exactly one blank line, so the
        transcript reads as airy, self-contained blocks. Callers just commit blocks — they
        never emit blank lines themselves.
        """
        width = self._cols()
        parts = [block.render(width) for block in self._blocks]
        # Each rendered part ends in "\n"; joining with "\n" inserts one blank line between them.
        self._transcript = "\n".join(parts)
        starts: list[int] = []
        line = 0
        for i, part in enumerate(parts):
            if i:
                line += 1  # the blank-line separator that precedes this block
            starts.append(line)
            line += part.count("\n")
        self._block_starts = starts

    def _status_text(self) -> list[tuple[str, str]]:
        """The docked status bar: the composed HUD rows, or the minimal static line if off.

        Deliberately hint-free — no esc/​/help/​^C clutter. The 'esc to interrupt' hint lives
        in the loading line, shown only while a turn runs (where it's actionable).
        """
        if not self._hud_rows:  # HUD disabled → the pre-HUD minimal static line
            autonomy = self._context.get("autonomy", "auto")
            rest = "  ·  ".join(
                p for p in (self._context.get("model", ""), self._context.get("branch", "")) if p
            )
            frags: list[tuple[str, str]] = [("class:status.key", f" {autonomy} mode")]
            if rest:
                frags.append(("class:status", f"  ·  {rest}"))
            frags.append(("class:status", " "))
            return frags
        lines = self._hud.compose(
            self._hud_rows, self.hud_state, width=self._cols(), separator=self._hud_sep
        )
        out: list[tuple[str, str]] = []
        for i, line in enumerate(lines):
            if i:
                out.append(("class:status", "\n"))
            out.append(("class:status", " "))  # 1-col left margin, like the transcript
            out.extend(line)
        return out or [("class:status", " ")]

    def _live_content(self) -> list[tuple[str, str]]:
        """Compose the live-region fragments from current state (called each paint)."""
        if self._modal_hint is not None:
            return self._modal_hint
        if self._activity is None:
            return []
        elapsed = time.monotonic() - self._turn_started
        frame = theme.SPINNER_FRAMES[self._frame_i % len(theme.SPINNER_FRAMES)]
        return live_region.live_fragments(self._activity, frame, elapsed, self._todos, self._cols())

    def _build_layout(self) -> Layout:
        live = ConditionalContainer(
            Window(FormattedTextControl(self._live_content), dont_extend_height=True),
            filter=Condition(lambda: self._modal_hint is not None or self._activity is not None),
        )
        glyph = Window(
            FormattedTextControl([("class:prompt", f"{theme.GLYPH_PROMPT} ")]),
            width=2,
            dont_extend_width=True,
            height=1,
        )
        # The input grows with content (1 → MAX_INPUT_LINES lines), then scrolls within.
        entry = Window(
            BufferControl(
                buffer=self._input,
                input_processors=[
                    ConditionalProcessor(PasswordProcessor(), Condition(lambda: self._secret_mode))
                ],
                include_default_input_processors=True,
            ),
            height=Dimension(min=1, max=MAX_INPUT_LINES),
            wrap_lines=True,
        )
        # A bordered input box (a distinct widget) so the input reads as separate from the
        # transcript above and the status bar below — not one clumped block.
        input_box = Frame(VSplit([Window(width=1), glyph, entry]), style="class:inputframe")
        status_height = max(1, len(self._hud_rows))  # 1 line (minimal/essential/off), 2 (full)
        status = Window(
            FormattedTextControl(self._status_text), height=status_height, style="class:status"
        )
        body = FloatContainer(
            HSplit([self._transcript_window, live, input_box, status]),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8))],
        )
        return Layout(body, focused_element=entry)

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        @kb.add("c-d")
        def _quit(event: Any) -> None:
            if self._modal is not None and not self._modal.done():
                return  # a gate is pending — a stray quit key must not exit
            self._exiting = True  # blocks any queued auto-resume from spawning at shutdown
            if self._task is not None and not self._task.done():
                self._task.cancel()  # cancel the in-flight turn, then quit
            event.app.exit()

        @kb.add("escape", eager=True)
        def _interrupt(event: Any) -> None:
            now = time.monotonic()
            double = (now - self._last_esc) < _DOUBLE_ESC_S
            self._last_esc = now
            if double:  # a quick second Esc clears whatever is in the input
                self._input.reset()
                return
            if self._task is not None and not self._task.done():
                self._task.cancel()
                if self._on_interrupt is not None:
                    # Tell the daemon to stop the turn too (off-loop; blocking HTTP). Without
                    # this, the daemon keeps running and the next turn hits "already running".
                    asyncio.get_running_loop().run_in_executor(None, self._on_interrupt)
                if self._last_turn_text and not self._input.text:
                    # Refill the input with the interrupted turn's text so it can be edited
                    # and resent (only if the user hasn't already started typing something new).
                    self._input.text = self._last_turn_text
                    self._input.cursor_position = len(self._last_turn_text)

        @kb.add("enter")
        def _enter(event: Any) -> None:
            # Enter submits (the buffer is multiline, so this overrides the default newline).
            # With a completion highlighted, Enter INCLUDES it as-is — a folder is attached
            # whole (Tab is what descends into it) — and ends the mention with a space so the
            # menu doesn't reopen. A second Enter (nothing highlighted) then submits.
            cs = self._input.complete_state
            if cs is not None and cs.current_completion is not None:
                self._input.apply_completion(cs.current_completion)
                self._input.insert_text(" ")
            else:
                self._input.validate_and_handle()

        @kb.add("tab")
        def _tab_complete(event: Any) -> None:
            # Tab drives @-file / /command completion: accept the highlighted match (or the
            # first, if none is highlighted). Accepting a folder (its text ends with "/")
            # re-opens completion on its contents, so Tab keeps descending; accepting a file
            # just inserts it. Arrow keys move the selection within the menu (prompt_toolkit
            # default). Nothing to complete → a no-op (the chat input has no other use for Tab).
            buf = self._input
            cs = buf.complete_state
            if cs is None:
                buf.start_completion(select_first=True)
                return
            if not cs.completions:
                return
            idx = cs.complete_index if cs.complete_index is not None else 0
            completion = cs.completions[idx]
            buf.apply_completion(completion)
            if completion.text.endswith("/"):
                buf.start_completion(select_first=False)  # folder → descend into its contents
            else:
                buf.insert_text(" ")  # file / command → select it and end the mention

        @kb.add("c-j")
        def _newline(event: Any) -> None:
            self._input.insert_text("\n")  # Ctrl-J inserts a literal newline (multi-line input)

        @kb.add("c-o")
        def _expand_output(event: Any) -> None:
            self.expand_output()  # Ctrl-O expands the most recent command's full output

        # Single-keypress gate answers (no Enter). Active only while a gate awaits and we're
        # not typing a refinement: 'y'/'n' resolve immediately; 'e' (plan only) starts editing;
        # any other key is swallowed so the input can't collect stray text during the gate.
        gate_key = Condition(lambda: self._modal is not None and not self._modal.done())
        gate_choose = gate_key & Condition(lambda: not self._modal_edit)
        plan_edit = gate_choose & Condition(
            lambda: self._modal_seg is not None and self._modal_seg.kind == "plan"
        )

        @kb.add("y", filter=gate_choose)
        @kb.add("Y", filter=gate_choose)
        def _gate_yes(event: Any) -> None:
            self._resolve_gate("y")

        @kb.add("n", filter=gate_choose)
        @kb.add("N", filter=gate_choose)
        def _gate_no(event: Any) -> None:
            self._resolve_gate("n")

        @kb.add("e", filter=plan_edit)
        @kb.add("E", filter=plan_edit)
        def _gate_edit(event: Any) -> None:
            self._modal_edit = True  # switch to typing the refinement (resolved on Enter)
            self._modal_hint = [("class:amber", " type your change, then Enter · esc to cancel")]
            self.app.invalidate()

        @kb.add("<any>", filter=gate_choose)
        def _gate_swallow(event: Any) -> None:
            pass  # ignore stray keys while a y/n/e gate waits (specific y/n/e/exit keys win)

        @kb.add("pageup")
        def _pgup(event: Any) -> None:
            info = self._transcript_window.render_info
            self._scroll_by(-max(1, (info.window_height - 1) if info is not None else 10))

        @kb.add("pagedown")
        def _pgdn(event: Any) -> None:
            info = self._transcript_window.render_info
            self._scroll_by(max(1, (info.window_height - 1) if info is not None else 10))

        @kb.add(Keys.BracketedPaste)
        def _paste(event: Any) -> None:
            # Normalize line endings first: a terminal paste often uses \r (or \r\n) as the
            # separator, which otherwise reads as one giant "line" (and crashed the path check).
            data = event.data.replace("\r\n", "\n").replace("\r", "\n")
            mention = paste.is_existing_path(data, self._cwd)
            if mention is not None:
                self._input.insert_text(f"@{mention} ")  # reuse the @-file attachment path
            elif paste.should_collapse(data):
                self._input.insert_text(self._pastes.add(data))  # stash behind a placeholder
            else:
                self._input.insert_text(data)

        @kb.add("backspace")
        def _backspace(event: Any) -> None:
            token = paste.trailing_placeholder(self._input.document.text_before_cursor)
            if token is not None:
                self._input.delete_before_cursor(count=len(token))  # remove the whole paste
                self._pastes.forget(token)
            else:
                self._input.delete_before_cursor(count=1)

        return kb

    def _resolve_gate(self, key: str) -> None:
        """Resolve a pending gate from a single keypress (y/n)."""
        if self._modal is not None and not self._modal.done():
            seg = self._modal_seg
            self._modal.set_result(parse_gate_answer(seg, key) if seg is not None else Answer("no"))

    def _on_accept(self, buff: Buffer) -> bool:
        # Return False so prompt_toolkit appends the (non-empty) text to history and then
        # resets the buffer — appending happens AFTER this handler, so we must NOT reset here
        # (resetting first would record empty history entries and break ↑/↓ recall).
        # A pending gate is answered by single keys (y/n/e); Enter only submits a refinement
        # that was started with 'e' (edit mode). A stray Enter during a gate is ignored.
        if self._modal is not None and not self._modal.done():
            if self._modal_edit:
                text = buff.text.strip()
                if self._secret_mode:
                    # Resolve, then wipe the buffer ourselves: prompt_toolkit's
                    # validate_and_handle() calls append_to_history() UNCONDITIONALLY after
                    # this handler returns, but append_to_history() only records non-empty
                    # text — and buff.reset() (called here, before we return) already
                    # cleared it, so the secret never touches .jack/cli_history. The
                    # `return True` doesn't gate that append; it only tells
                    # validate_and_handle() to skip its own redundant post-handler reset().
                    self._modal.set_result(Answer("refine", text) if text else Answer("reject"))
                    buff.reset()
                    return True
                self._modal.set_result(Answer("refine", text) if text else Answer("reject"))
            return False
        text = buff.text.strip()
        if not text or self.busy:
            return False
        self._last_turn_text = text  # so esc can refill it for editing/resending
        self._follow = True  # jump to the bottom for a freshly-submitted turn (see the reply)
        self._task = asyncio.create_task(self._drive(text, self._turn_no))
        self._turn_no += 1
        return False

    async def _drive(self, text: str, turn_no: int) -> None:
        self._turn_started = time.monotonic()
        self._frame_i = 0
        self._ticker = asyncio.create_task(self._tick())
        try:
            await self._run_turn(text, turn_no)
        except asyncio.CancelledError:
            pass
        except Exception:  # a failed turn must never kill the app loop
            _log.exception("turn crashed turn_no=%d", turn_no)
        finally:
            if self._ticker is not None:
                self._ticker.cancel()
                self._ticker = None
            self._activity = None
            self._todos = []  # the live checklist vanishes with the live region at turn end
            self._modal_hint = None
            self._secret_mode = False
            self._modal = None
            self._modal_seg = None
            self.app.invalidate()
            if not self._exiting:
                asyncio.get_running_loop().call_soon(self._maybe_pickup)

    async def _tick(self) -> None:
        """Advance the spinner frame and repaint the elapsed timer while a turn runs."""
        try:
            while True:
                self._frame_i += 1
                self.app.invalidate()
                await asyncio.sleep(_TICK_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    async def begin_modal(self, seg: Segment) -> Answer:
        """Show a gate affordance in the live region and await the typed answer."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Answer] = loop.create_future()
        self._modal = fut
        self._modal_seg = seg
        self._modal_edit = seg.kind in ("input", "secret")
        self._secret_mode = seg.kind == "secret"
        if seg.kind in ("input", "secret"):
            hint = f"{seg.text}  (enter submits · empty cancels)"
        elif seg.kind == "plan":
            hint = "[y]es · [n]o · [e]dit"
        else:
            hint = "Approve? [y]es · [n]o"
        self._modal_hint = [("class:amber", f" {hint}")]
        self.app.invalidate()
        try:
            return await fut
        finally:
            self._modal_hint = None
            self._modal = None
            self._modal_seg = None
            self._modal_edit = False
            self._secret_mode = False
            self.app.invalidate()

    def append_transcript(self, renderable: Any) -> None:
        """Append a finished renderable (rich → ANSI) as a plain block; tail only if following."""
        try:
            ansi = render_ansi(renderable, self._cols())
        except Exception:  # a bad renderable must never crash the turn
            _log.exception("rendering a transcript line failed; dropping it")
            return
        # Do NOT force-follow: if the user scrolled up to read, new output must not yank them
        # to the bottom — following resumes only on a fresh turn or on scrolling to the bottom.
        self._blocks.append(_PlainBlock(ansi))
        self._recompose()  # the composer inserts the blank-line separator between blocks
        self.app.invalidate()

    def commit_command_block(self, label: str, output: list[str], *, gated: bool) -> None:
        """Append a finished command as a collapsible block (compact card; ^O toggles output)."""
        self._blocks.append(_CommandBlock(label, list(output), gated))
        self._recompose()
        self.app.invalidate()

    def clear_transcript(self) -> None:
        """Empty the transcript region (the ``/clear`` command)."""
        self._blocks = []
        self._recompose()
        self.app.invalidate()

    def expand_pastes(self, text: str) -> str:
        """Expand any ``[Pasted #N]`` placeholders in ``text`` back to their real content."""
        return self._pastes.expand(text)

    def expand_output(self, index: int | None = None) -> bool:
        """Toggle command output expansion in place, preserving the view position (^O / /output).

        No ``index`` (``^O`` / bare ``/output``): toggle *all* commands — collapse everything if
        it's all expanded, else expand everything. With ``index`` (``/output N``): toggle just the
        Nth command's output. Returns ``False`` when there are no commands to toggle.
        """
        commands = [b for b in self._blocks if isinstance(b, _CommandBlock)]
        if not commands:
            return False
        if index is None:
            target = not all(b.expanded for b in commands)  # collapse-all if all open, else open

            def _apply_all() -> None:
                for block in commands:
                    block.expanded = target

            self._recompose_preserving_view(_apply_all)
            return True
        i = index - 1
        if not 0 <= i < len(commands):
            return False

        def _toggle_one() -> None:
            commands[i].expanded = not commands[i].expanded

        self._recompose_preserving_view(_toggle_one)
        return True

    def _recompose_preserving_view(self, mutate: Callable[[], Any]) -> None:
        """Apply ``mutate`` (flip expand flags), recompose, and keep the viewport anchored.

        When following, stay pinned to the bottom. Otherwise anchor on the block the viewport
        currently rests on: after content above it grows/shrinks, shift ``_scroll_pos`` by the
        same delta so that block stays at the same screen row — the view never jumps.
        """
        if self._follow:
            mutate()
            self._recompose()
            self.app.invalidate()
            return
        bi, within = self._locate(self._cursor_y())  # anchor block + offset, in the old layout
        mutate()
        self._recompose()
        new_start = self._block_starts[bi] if 0 <= bi < len(self._block_starts) else 0
        self._scroll_pos = max(0, new_start + within)
        self.app.invalidate()

    def _locate(self, line: int) -> tuple[int, int]:
        """Return the block index containing ``line`` and the line offset within that block."""
        starts = self._block_starts
        if not starts:
            return 0, 0
        bi = 0
        for i, start in enumerate(starts):
            if start <= line:
                bi = i
            else:
                break
        return bi, max(0, line - starts[bi])

    def set_activity(self, text: str) -> None:
        """Set the live region's sub-activity line (the app owns the spinner + verb + timer)."""
        self._activity = text
        self.app.invalidate()

    def set_todos(self, todos: list[tuple[str, str]]) -> None:
        """Replace the live checklist (``(status, step)`` rows) shown under the spinner."""
        self._todos = list(todos)
        self.app.invalidate()

    def clear_activity(self) -> None:
        """Clear the live region."""
        self._activity = None
        self._todos = []
        self.app.invalidate()

    def on_task_finished(self, events: list[dict[str, Any]]) -> None:
        """Queue finished background tasks and, when idle, run a continuation turn.

        Thread-safe: called from the events-listener thread. Finished-while-busy events are
        buffered and delivered when the current turn ends (see :meth:`_maybe_pickup`).
        """
        if not events:
            return
        loop = self.app.loop
        if loop is None:
            return

        def _enqueue() -> None:
            self._pending_pickups.extend(events)
            self._maybe_pickup()

        try:
            loop.call_soon_threadsafe(_enqueue)
        except RuntimeError:  # loop closing at shutdown — nothing to resume onto
            _log.debug("could not schedule task pickup (loop closing)")

    def on_mcp_event(self, evt: dict[str, Any]) -> None:
        """Commit a one-line MCP status/OAuth update to the transcript (thread-safe).

        Fired on the events-listener thread; hops to the app loop. Quiet by design:
        ``render_mcp_event`` returns ``None`` for chatter (e.g. disconnects), and a
        closed loop at shutdown is ignored.
        """
        loop = self.app.loop
        if loop is None:
            return
        from autobot.cli import mcp_render

        line = mcp_render.render_mcp_event(evt)
        if line is None:
            return
        try:
            loop.call_soon_threadsafe(self.append_transcript, line)
        except RuntimeError:  # loop closing at shutdown
            _log.debug("dropped mcp event line (loop closing)")

    def update_hud(self, **fields: object) -> None:
        """Update HUD state fields and repaint (safe to call from any thread).

        Mirrors :meth:`on_task_finished`: when the app loop is running the update hops onto it
        via ``call_soon_threadsafe``; before the loop exists (startup seeding, unit tests) it is
        applied synchronously and the repaint is skipped (the first paint happens on run).
        """

        def _apply() -> None:
            for name, value in fields.items():
                if hasattr(self.hud_state, name):
                    setattr(self.hud_state, name, value)
            if self.app.is_running:
                self.app.invalidate()

        loop = self.app.loop
        if loop is None:
            _apply()
            return
        try:
            loop.call_soon_threadsafe(_apply)
        except RuntimeError:  # loop closing at shutdown
            _log.debug("dropped HUD update (loop closing)")

    def on_context_event(self, evt: dict[str, Any]) -> None:
        """Feed a context-usage payload (``used``/``window``/``model``) into the HUD.

        ``evt`` is the ``ctx`` block from ``GET /coder/usage`` (the same shape the orb's meter
        consumes), pulled by the turn-end HUD refresh; drives the context% + tokens segments.
        """
        self.update_hud(
            used=int(evt.get("used", 0) or 0),
            window=int(evt.get("window", 0) or 0),
            model=str(evt.get("model", "") or self.hud_state.model),
        )

    def _maybe_pickup(self) -> None:
        """If idle and pickups are queued, notice them and run a continuation turn."""
        if self._exiting or self.busy or not self._pending_pickups:
            return
        from autobot.cli import render

        events = self._pending_pickups[:]
        self._pending_pickups.clear()
        self._follow = True  # jump to the bottom so the auto-resume notice + turn are visible
        self.append_transcript(render.render_task_pickup(events))
        self._task = asyncio.create_task(self._drive(_CONTINUATION_PROMPT, self._turn_no))
        self._turn_no += 1

    async def run_async(self) -> None:
        """Run the app to completion (Ctrl-C / Ctrl-D at idle exits)."""
        await self.app.run_async()


class AppSurface:
    """The real ``Surface``: appends to the transcript and paints the app's live region."""

    def __init__(self, japp: JackApp, console: Any = None) -> None:
        """Bind to a ``JackApp``. ``console`` is unused (kept for call-site compatibility)."""
        self._japp = japp

    def commit(self, renderable: Any) -> None:
        """Commit a finished renderable into the scrollable transcript."""
        self._japp.append_transcript(renderable)

    def commit_command(self, label: str, output: list[str], *, gated: bool) -> None:
        """Append a finished command as a collapsible block (compact card; ^O toggles output).

        In confirm mode (``gated``) the permission gate already showed the command, so the card
        is result-only; in auto mode the card leads with the command so it's shown once.
        """
        self._japp.commit_command_block(label, output, gated=gated)

    def set_activity(self, text: str) -> None:
        """Set the live region's current-activity line."""
        self._japp.set_activity(text)

    def set_todos(self, todos: list[tuple[str, str]]) -> None:
        """Replace the live checklist shown under the spinner."""
        self._japp.set_todos(todos)

    def clear_activity(self) -> None:
        """Clear the live region."""
        self._japp.clear_activity()

    async def ask(self, seg: Segment) -> Answer:
        """Resolve a plan/permission gate via the pinned-input modal handshake."""
        if seg.kind == "pending":
            from rich.text import Text

            from autobot.cli.render import format_command_gate

            # Pretty-print a chained command line-by-line so it's reviewable before approving.
            self.commit(Text(format_command_gate(seg.text), style="amber"))
        return await self._japp.begin_modal(seg)
