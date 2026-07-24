"""Render a :class:`~autobot.cli.classify.Segment` to plain text or a rich renderable.

Plain forms back the one-shot mode and keep rendering testable without a TTY. Rich forms
back the inline shell: the ⏺ assistant gutter, the plan and permission prompts, the welcome
banner, the footer byline, and full-width diffs (via ``diffview``). The user's own turn is
the input-prompt line the shell already leaves in the scrollback, so it isn't re-rendered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from autobot.cli import theme
from autobot.cli.classify import Segment

if TYPE_CHECKING:
    from rich.console import RenderableType


def render_plain(seg: Segment) -> str:
    """Plain-text rendering of a segment (no ANSI, no external deps)."""
    if seg.kind == "plan":
        return f"PLAN\n{seg.text}"
    if seg.kind == "error":
        return f"Error: {seg.text}"
    if seg.kind == "token":
        return seg.text
    if seg.kind == "tool":
        return f"{theme.NEST_INDENT}{seg.text}"
    return seg.text  # pending / done


def render_rich(seg: Segment) -> RenderableType:
    """Dispatch a segment to its rich renderable by kind."""
    if seg.kind == "plan":
        return render_plan_card(seg.text)
    if seg.kind == "pending":
        return render_permission_card(seg.text)
    if seg.kind == "error":
        from rich.text import Text

        return Text(f"Error: {seg.text}", style="red")
    if seg.kind == "token":
        from rich.text import Text

        return Text(seg.text)
    if seg.kind == "tool":
        return render_tool(seg)
    return render_reply(seg.text)  # done


def render_reply(text: str) -> RenderableType:
    """The assistant turn: a ⏺ gutter dot inline with the reply's markdown.

    A two-column ``Table.grid`` keeps the dot on the same row as the first line of the
    reply (a plain ``Columns`` layout drops it onto its own line).
    """
    from rich.markdown import Markdown
    from rich.table import Table
    from rich.text import Text

    grid = Table.grid(padding=(0, 1))
    grid.add_column()  # gutter
    grid.add_column()  # content
    grid.add_row(Text(theme.GLYPH_ASSISTANT, style="assistant"), Markdown(text))
    return grid


def render_tool(seg: Segment) -> RenderableType:
    """A dim, indented nested tool-activity line (a friendly verb, no connector glyph).

    Reads as ``  Read foo.py`` / ``  Searched "…"`` sitting under the reply — cleaner than the
    old ``⎿`` connector, which read as a cryptic corner rather than meaningful activity.
    """
    from rich.text import Text

    return Text(f"{theme.NEST_INDENT}{seg.text}", style="tool")


def render_task_pickup(events: list[dict[str, Any]]) -> RenderableType:
    """A notice that finished background task(s) are being auto-picked-up (auto-resume).

    Shown when the REPL was idle and a backgrounded command completed: the agent continues
    on its own with the result. Teal ``✓`` if all succeeded, amber ``✕`` if any failed.
    """
    from rich.text import Text

    ids = ", ".join(str(e.get("id", "task")) for e in events)
    ok = all(e.get("status") == "done" for e in events)
    mark = "✓" if ok else "✕"
    noun = "subagent" if all(e.get("kind") == "agent" for e in events) else "background"
    text = f"⚑ {noun} {ids} finished {mark} — picking it up…"
    return Text(text, style="teal" if ok else "amber")


def render_plan_card(reply: str) -> RenderableType:
    """The plan: the assistant's numbered plan, then the ``[y]es · [e]dit · [n]o`` choices.

    The reply already contains the numbered steps, so they are shown once (via the reply)
    and never re-listed.
    """
    from rich.console import Group
    from rich.text import Text

    choices = Text()
    choices.append("[y]es", style="teal")
    choices.append("  ·  ")
    choices.append("[e]dit", style="teal")
    choices.append("  ·  ")
    choices.append("[n]o", style="teal")
    return Group(render_reply(reply), Text(""), choices)


def render_permission_card(prompt_text: str) -> RenderableType:
    """The permission prompt: the (amber) request, then a plain ``[y/n]`` choice line."""
    from rich.console import Group
    from rich.text import Text

    choices = Text()
    choices.append("[y/n]", style="teal")
    return Group(Text(prompt_text, style="amber"), Text(""), choices)


def _split_command(cmd: str) -> list[tuple[str, str]]:
    """Split a shell command into ``(segment, connector)`` at top-level ``;`` / ``&&`` / ``||``.

    Quote-aware (splits are ignored inside ``'…'`` / ``"…"``) and pipes (single ``|``) are left
    inline — a pipeline is one logical step. Used only to pretty-print a command for preview.
    """
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
        elif c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
        elif cmd[i : i + 2] in ("&&", "||"):
            parts.append(("".join(buf).strip(), cmd[i : i + 2]))
            buf, i = [], i + 2
        elif c == ";":
            parts.append(("".join(buf).strip(), ";"))
            buf, i = [], i + 1
        else:
            buf.append(c)
            i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append((tail, ""))
    return parts


def format_command_gate(prompt: str) -> str:
    """Reformat a ``Run this command?`` prompt so a chained command reads line-by-line.

    The daemon builds the prompt with the command on one ``  $ …`` line; a long ``;``/``&&``/``||``
    chain is hard to scan there. This splits that line into one sub-command per line (connectors
    kept as trailing markers) so the user can review it before approving. Any other prompt (no
    ``$`` command line, or a single command) is returned unchanged.
    """
    out: list[str] = []
    for line in prompt.split("\n"):
        if line.strip().startswith("$ "):
            segments = _split_command(line.strip()[2:])
            if len(segments) > 1:
                for idx, (segment, connector) in enumerate(segments):
                    prefix = "  $ " if idx == 0 else "    "
                    out.append(f"{prefix}{segment}{f' {connector}' if connector else ''}")
                continue
        out.append(line)
    return "\n".join(out)


def render_welcome(ctx: dict[str, str]) -> RenderableType:
    """The startup banner: just the cwd + a tip (no title line).

    Deliberately minimal and clean: model, autonomy, and branch live in the always-visible
    bottom status bar (no duplication), and the left margin is applied by the transcript
    renderer — so no leading spaces here. Only the cwd and the tip appear.
    """
    from rich.console import Group
    from rich.text import Text

    cwd = Text(ctx.get("cwd", ""), style="dim")
    tip = Text("💡 Tip  type / for commands & skills, @ to add a file", style="dim")
    return Group(cwd, Text(""), tip)


def render_footer(ctx: dict[str, str], width: int) -> str:
    """A width-gated ``model · autonomy · branch · cwd`` status byline (plain str)."""
    parts = [
        ctx.get("model", ""),
        ctx.get("autonomy", ""),
        ctx.get("branch", ""),
        ctx.get("cwd", ""),
    ]
    full = "  ·  ".join(p for p in parts if p)
    if len(full) <= width:
        return full
    short = "  ·  ".join(p for p in (ctx.get("autonomy", ""), ctx.get("branch", "")) if p)
    return short[:width]


def render_diff_plain(diff: str) -> str:
    """Plain diff: return as-is (already unified-diff text)."""
    return diff


def render_diff_rich(diff: str, *, width: int = 80) -> RenderableType:
    """Rich diff: full-width colored bars with word-level highlight (via diffview)."""
    from autobot.cli import diffview

    return diffview.render_diff(diff, width=width)


def _fmt_mtime(mtime: object) -> str:
    """Format an epoch-seconds mtime as ``YYYY-MM-DD HH:MM`` (empty on bad input)."""
    from datetime import datetime

    try:
        return datetime.fromtimestamp(float(mtime)).strftime(  # type: ignore[arg-type]
            "%Y-%m-%d %H:%M"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _short_home(path: str) -> str:
    """Replace the home prefix with ``~`` (mirrors the shell's cwd shortening)."""
    from pathlib import Path

    home = str(Path.home())
    return path.replace(home, "~", 1) if path.startswith(home) else path


def render_sessions(rows: list[dict[str, object]]) -> RenderableType:
    """A table of stored sessions (id · model · cwd · modified), newest first."""
    from rich.table import Table
    from rich.text import Text

    if not rows:
        return Text("No sessions yet.", style="dim")
    # Table.header_style is resolved eagerly (unlike Text's style=, resolved lazily), so a
    # bare theme-name string raises MissingStyle on a themeless console (e.g. in tests);
    # look the color up from the theme dict instead of hardcoding the hex here.
    table = Table(show_header=True, header_style=theme.STYLES["teal"], box=None, padding=(0, 2))
    for col in ("id", "model", "cwd", "modified"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r.get("id", ""))[:8],
            str(r.get("model", "")),
            _short_home(str(r.get("cwd", ""))),
            _fmt_mtime(r.get("mtime")),
        )
    return table


def render_checkpoints(rows: list[dict[str, str]]) -> RenderableType:
    """A newest-first list of checkpoints as ``<n>  <label>`` lines."""
    from rich.text import Text

    if not rows:
        return Text("No checkpoints.", style="dim")
    out = Text()
    for i, r in enumerate(rows):
        n = r.get("ref", "").rsplit("/", 1)[-1]
        out.append(f"{n}", style="teal")
        out.append(f"  {r.get('label', '')}")
        if i < len(rows) - 1:
            out.append("\n")
    return out


def _fmt_usd(bucket: dict[str, Any]) -> str:
    """`$X.XXXX`, prefixed `≥` when some rows were unpriced."""
    usd = float(bucket.get("usd", 0.0) or 0.0)
    prefix = "≥ " if bucket.get("has_unpriced") else ""
    return f"{prefix}${usd:.4f}"


def render_cost(payload: dict[str, Any], width: int) -> RenderableType:
    """A compact usage summary: this session + today/7d/all-time + top models/workspaces."""
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    rollups = payload.get("rollups") or {}
    totals = rollups.get("totals") or {}
    if not totals or totals.get("all_time", {}).get("turns", 0) == 0:
        return Text("No usage recorded yet. Run a turn, then try /cost again.", style="dim")

    parts: list[RenderableType] = []
    session = rollups.get("session")
    model = payload.get("model") or "?"
    provider = payload.get("provider") or "?"
    if session:
        parts.append(
            Text(
                f"This session · {model} ({provider}) · {session['turns']} turns · "
                f"in {session['in']:,} / out {session['out']:,} · "
                f"cache r {session['cache_read']:,} / w {session['cache_write']:,} · "
                f"{_fmt_usd(session)}",
                style="teal",
            )
        )

    table = Table(show_header=True, header_style="dim", expand=False, pad_edge=False)
    table.add_column("Window")
    table.add_column("Turns", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cache r/w", justify="right")  # cache tokens ARE billed (included in Cost)
    table.add_column("Cost", justify="right")
    for label, key in (("Today", "today"), ("Last 7 days", "last_7d"), ("All time", "all_time")):
        b = totals.get(key)
        if b:
            cache = f"{b['cache_read']:,}/{b['cache_write']:,}"
            table.add_row(label, f"{b['turns']:,}", f"{b['tokens']:,}", cache, _fmt_usd(b))
    parts.append(table)

    models = rollups.get("by_model") or []
    if models:
        top = "  ".join(f"{m['key']} {_fmt_usd(m)}" for m in models[:3])
        parts.append(Text(f"Top models: {top}", style="dim"))
    parts.append(
        Text(
            "Cache read/write tokens are billed and included in Cost. List prices "
            "(no promo assumed — actual may be lower); the provider console is authoritative. "
            "/cost open for the full dashboard.",
            style="dim",
        )
    )
    return Group(*parts)
