"""prompt_toolkit input pieces: ``Answer``, the ``/``+``@`` completer, and the auto-suggester.

The ``Answer`` value object, the completer, and the auto-suggester are all unit-tested. The
interactive shell itself lives in :mod:`autobot.cli.app` (one long-lived Application); this
module only owns the reusable input pieces it composes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

_META_MAX = 60  # skill descriptions can be long; the menu's meta column shows a short head
_LAST_TOKEN_RE = re.compile(r"\S*$")  # the run of non-whitespace just before the cursor


def _last_token(text: str) -> str:
    """The whitespace-delimited token ending at the cursor (``""`` right after a space)."""
    match = _LAST_TOKEN_RE.search(text)
    return match.group(0) if match else ""


def _short_meta(text: str, limit: int = _META_MAX) -> str:
    """A one-line, length-capped description for the completion menu's meta column."""
    line = " ".join(text.split())  # collapse newlines / runs so the menu row stays one line
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class Answer:
    """A parked-turn answer: the reply ``value`` (+ optional refine ``text``)."""

    value: str
    text: str = ""


class JackCompleter(Completer):
    """Completes ``/command`` + ``/skill`` and ``@path`` for the token at the cursor.

    A ``/`` token at the **start** of the line completes built-in commands *and* skills; a
    ``/`` token **mid-line** (after other text) completes skills only — so a control command
    like ``/exit`` is never surfaced where the user is composing prose (and, because a command
    only dispatches when it is the whole line, an accidental pick there can't act).
    """

    def __init__(
        self, commands: dict[str, str], cwd: str, skills: Sequence[tuple[str, str]] = ()
    ) -> None:
        """Store the command set, the ``@``-path root, and the ``(name, description)`` skills."""
        self._commands = commands
        self._cwd = cwd
        self._skills = list(skills)

    def get_completions(  # noqa: D102
        self, document: Document, complete_event: object
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        word = _last_token(text)
        if word.startswith("/"):
            at_start = not text[: len(text) - len(word)].strip()
            yield from self._slash(word, at_start=at_start)
            return
        if word.startswith("@"):
            yield from self._files(word[1:])

    def _slash(self, word: str, *, at_start: bool) -> Iterable[Completion]:
        """Command (line-start only) + skill completions matching ``word`` (incl. its ``/``)."""
        if at_start:
            for name, desc in self._commands.items():
                if name.startswith(word):
                    yield Completion(name, start_position=-len(word), display_meta=desc)
        for name, desc in self._skills:
            slash = f"/{name}"
            if slash.startswith(word):
                yield Completion(
                    slash,
                    start_position=-len(word),
                    display=slash,
                    display_meta=f"skill · {_short_meta(desc)}",
                )

    def _files(self, prefix: str) -> Iterable[Completion]:
        """Complete an ``@path`` segment, descending into subfolders, with type icons.

        ``prefix`` is the text after ``@`` (e.g. ``src/cli/pro``). Only the final segment is
        completed (``pro`` -> ``prompt.py``); folders gain a trailing ``/`` so the next
        keystroke keeps descending. Folders sort first, then files, each shown with a glyph
        and a type in the meta column.
        """
        dirpart, _, partial = prefix.rpartition("/")
        base = Path(self._cwd) / dirpart if dirpart else Path(self._cwd)
        try:
            entries = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            if not entry.name.startswith(partial):
                continue
            is_dir = entry.is_dir()
            icon, kind = _icon_for(entry, is_dir=is_dir)
            suffix = "/" if is_dir else ""
            yield Completion(
                entry.name + suffix,
                start_position=-len(partial),
                display=f"{icon} {entry.name}{suffix}",
                display_meta=kind,
            )


# Type glyphs by extension for the @-path completer (folder handled separately). Terminal
# emoji, so any font renders them; the meta column carries the word for clarity.
_ICONS: dict[str, tuple[str, str]] = {
    ".png": ("🖼️", "image"),
    ".jpg": ("🖼️", "image"),
    ".jpeg": ("🖼️", "image"),
    ".gif": ("🖼️", "image"),
    ".webp": ("🖼️", "image"),
    ".svg": ("🖼️", "image"),
    ".bmp": ("🖼️", "image"),
    ".ico": ("🖼️", "image"),
    ".pdf": ("📕", "pdf"),
    ".doc": ("📘", "doc"),
    ".docx": ("📘", "doc"),
    ".xls": ("📊", "sheet"),
    ".xlsx": ("📊", "sheet"),
    ".csv": ("📊", "sheet"),
    ".md": ("📝", "markdown"),
    ".json": ("🔧", "config"),
    ".yaml": ("🔧", "config"),
    ".yml": ("🔧", "config"),
    ".toml": ("🔧", "config"),
}


def _icon_for(path: Path, *, is_dir: bool) -> tuple[str, str]:
    """The (glyph, type-word) for a completion entry."""
    if is_dir:
        return ("📁", "folder")
    return _ICONS.get(path.suffix.lower(), ("📄", "file"))


class JackAutoSuggest(AutoSuggest):
    """Inline ghost text: the tail of the completer's top match for the token at the cursor.

    Reuses :class:`JackCompleter` as the single source of truth, so the suggestion obeys the
    same rules — skills mid-line, never a control command mid-line, ``@`` paths. It shows greyed
    after the cursor; ``→`` / ``End`` / ``^E`` accept it (wired in the app's key bindings).
    """

    def __init__(self, completer: JackCompleter) -> None:
        """Bind to the completer whose top candidate becomes the ghost suggestion."""
        self._completer = completer

    def get_suggestion(self, buffer: Buffer, document: Document) -> Suggestion | None:
        """The remaining text of the top ``/`` or ``@`` completion for this token, or ``None``."""
        word = _last_token(document.text_before_cursor)
        if not word.startswith(("/", "@")):
            return None
        for comp in self._completer.get_completions(document, None):
            # ``comp.text`` replaces the last ``-start_position`` typed chars; the ghost is the
            # rest (what pressing → / Tab would add). Take only the first (best) candidate.
            typed = -comp.start_position
            remaining = comp.text[typed:] if typed else comp.text
            return Suggestion(remaining) if remaining else None
        return None
