"""Pure composer for the live region: a spinner action line plus a windowed todo checklist.

No terminal I/O and no threads — it only turns state (the current-action label, frame char,
elapsed, and the model's todo list) into prompt_toolkit ``(style, text)`` fragments. The
running Application paints these; that single owner is what removes the threaded-``Live``
write race (rich#1530). Keeping the todos here (under the spinner) rather than committing a
line per delta keeps the transcript clean — the checklist updates in place while the turn runs.
"""

from __future__ import annotations

# Raw tool name → a short present-continuous label shown as the spinner's current action.
# Grouped by intent (not one entry per tool) so the spinner stays legible, not a tool dump.
_ACTIONS: dict[str, str] = {
    "read_file": "Reading file",
    "grep": "Searching",
    "glob": "Listing files",
    "list_dir": "Listing files",
    "run_command": "Running command",
    "write_file": "Editing file",
    "edit_file": "Editing file",
    "multi_edit": "Editing file",
    "spawn_agent": "Running subagent",
    "repo_map": "Mapping the repo",
    "update_plan": "Planning",
    "skill": "Loading skill",
    "read_skill_file": "Reading skill file",
}

# The label shown while the model is thinking (no tool running yet).
DEFAULT_ACTION = "Working"

# status → (glyph, style class). Anything else (e.g. "pending") falls back to _PENDING.
_MARKS: dict[str, tuple[str, str]] = {
    "done": ("☑", "teal"),
    "in_progress": ("◐", "verb"),
    "blocked": ("⊘", "amber"),
}
_PENDING = ("☐", "dim")

# Show at most this many checklist rows under the spinner before collapsing (done/overflow).
MAX_TODO_ROWS = 5


def action_label(name: str) -> str:
    """Map a raw tool name to a short current-action label (``read_file`` → ``Reading file``).

    Falls back to a humanized form of the name (``repo_stats`` → ``Repo stats``) so an unmapped
    tool still reads sensibly rather than showing a raw identifier.
    """
    known = _ACTIONS.get(name)
    if known:
        return known
    return name.replace("_", " ").strip().capitalize() or DEFAULT_ACTION


def byline(elapsed_s: float, width: int) -> str:
    """A width-gated ``esc to interrupt · Ns`` byline (drops parts as width shrinks)."""
    secs = f"{int(elapsed_s)}s"
    full = f"esc to interrupt · {secs}"
    if width >= len(full) + 4:
        return full
    if width >= len(secs) + 4:
        return secs
    return ""


def todo_panel(
    todos: list[tuple[str, str]], width: int, *, max_rows: int = MAX_TODO_ROWS
) -> list[tuple[str, str]]:
    """Windowed checklist fragments (each a new indented row) shown under the spinner.

    ``todos`` is ``(status, step)`` in list order. With ``max_rows`` or fewer, every step is
    shown. With more, the done steps before the focused (in-progress) step collapse into one
    ``☑ N done`` summary, the focused step and the pending steps that fit are shown, and any
    overflow becomes a ``☐ +N more`` summary — so the focused step is always visible and the
    panel never exceeds ``max_rows`` rows.
    """
    if not todos:
        return []
    frags: list[tuple[str, str]] = []

    def row(glyph: str, style: str, text: str) -> None:
        frags.append(("", "\n "))  # own row, aligned to the 1-col left margin
        frags.append((f"class:{style}", f"  {glyph} {text[: max(1, width - 6)]}"))

    if len(todos) <= max_rows:
        for status, step in todos:
            glyph, style = _MARKS.get(status, _PENDING)
            row(glyph, style, step)
        return frags

    focus = next((i for i, (s, _) in enumerate(todos) if s == "in_progress"), None)
    if focus is None:  # nothing running → focus the first unfinished step (else the last)
        focus = next((i for i, (s, _) in enumerate(todos) if s != "done"), len(todos) - 1)
    done_before = sum(1 for i, (s, _) in enumerate(todos) if i < focus and s == "done")
    budget = max_rows - (1 if done_before else 0)
    window = todos[focus : focus + budget]
    if len(todos) - (focus + len(window)) > 0:  # reserve a row for the "+N more" summary
        window = window[:-1]
    remaining = len(todos) - (focus + len(window))

    if done_before:
        row("☑", "teal", f"{done_before} done")
    for status, step in window:
        glyph, style = _MARKS.get(status, _PENDING)
        row(glyph, style, step)
    if remaining > 0:
        row("☐", "dim", f"+{remaining} more")
    return frags


def live_fragments(
    label: str,
    frame_char: str,
    elapsed_s: float,
    todos: list[tuple[str, str]],
    width: int,
) -> list[tuple[str, str]]:
    """Compose the live region: a spinner action line, then the windowed todo checklist.

    Line 1 is ``<glyph> <label>…  ·  <byline>`` — the current action. Any ``todos`` render as
    indented rows below it (see :func:`todo_panel`). Styles are prompt_toolkit class names
    (resolved by the app's ``Style`` map), not rich styles.
    """
    frags: list[tuple[str, str]] = [
        ("", " "),  # left margin — aligns the spinner with the transcript's 1-col margin
        ("class:spinner", frame_char + " "),
        ("class:verb", f"{label}…"),
    ]
    tail = byline(elapsed_s, width)
    if tail:
        frags.append(("class:dim", f"  ·  {tail}"))
    frags.extend(todo_panel(todos, width))
    return frags
