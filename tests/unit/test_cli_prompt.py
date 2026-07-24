"""Choice parsers (pure) and the / + @ completer."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.cli import prompt


def test_slash_completer_offers_matching_commands() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    comp = prompt.JackCompleter({"/help": "h", "/clear": "c", "/exit": "e"}, cwd=".")
    doc = Document("/he", cursor_position=3)
    texts = [c.text for c in comp.get_completions(doc, None)]
    assert "/help" in texts and "/clear" not in texts


def test_file_completer_offers_cwd_paths(tmp_path: Path) -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    (tmp_path / "apples.py").write_text("x", encoding="utf-8")
    (tmp_path / "bananas.py").write_text("x", encoding="utf-8")
    comp = prompt.JackCompleter({}, cwd=str(tmp_path))
    doc = Document("fix @ap", cursor_position=7)
    texts = [c.text for c in comp.get_completions(doc, None)]
    assert any("apples.py" in t for t in texts)
    assert not any("bananas.py" in t for t in texts)


def test_file_completer_descends_into_subfolders(tmp_path: Path) -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    (tmp_path / "src" / "cli").mkdir(parents=True)
    (tmp_path / "src" / "cli" / "prompt.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "cli" / "render.py").write_text("x", encoding="utf-8")
    comp = prompt.JackCompleter({}, cwd=str(tmp_path))
    line = "open @src/cli/pro"
    doc = Document(line, cursor_position=len(line))
    texts = [c.text for c in comp.get_completions(doc, None)]
    assert "prompt.py" in texts  # nested file is reachable (the reported bug)
    assert "render.py" not in texts  # doesn't match the "pro" partial


def test_file_completer_shows_icons_and_folders_first(tmp_path: Path) -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    (tmp_path / "assets").mkdir()
    (tmp_path / "logo.png").write_text("x", encoding="utf-8")
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    comp = prompt.JackCompleter({}, cwd=str(tmp_path))
    doc = Document("@", cursor_position=1)
    comps = list(comp.get_completions(doc, None))
    assert comps[0].text == "assets/"  # folders sort first, with a trailing slash
    meta = {c.text: c.display_meta_text for c in comps}
    assert meta["assets/"] == "folder"
    assert meta["logo.png"] == "image"
    assert meta["notes.md"] == "markdown"
    display = {c.text: c.display_text for c in comps}
    assert "🖼️" in display["logo.png"] and "📁" in display["assets/"]


# --- skills in the / completer -------------------------------------------------------------

_SKILLS = [("deep-research", "Fan-out research report"), ("explain-code", "Walk through code")]


def _slash_texts(line: str) -> list[str]:
    from prompt_toolkit.document import Document

    comp = prompt.JackCompleter(
        {"/help": "h", "/clear": "c", "/exit": "e"}, cwd=".", skills=_SKILLS
    )
    doc = Document(line, cursor_position=len(line))
    return [c.text for c in comp.get_completions(doc, None)]


def test_slash_completer_line_start_offers_commands_and_skills() -> None:
    pytest.importorskip("prompt_toolkit")
    texts = _slash_texts("/")
    assert "/help" in texts  # built-in command
    assert "/deep-research" in texts  # skill, offered as /name
    assert "/explain-code" in texts


def test_slash_completer_filters_skills_by_prefix() -> None:
    pytest.importorskip("prompt_toolkit")
    texts = _slash_texts("/deep")
    assert texts == ["/deep-research"]  # only the matching skill (no command starts with /deep)


def test_slash_completer_midline_offers_skills_only() -> None:
    pytest.importorskip("prompt_toolkit")
    # A `/` typed after other text must surface skills but NEVER a control command like /exit,
    # so an accidental pick mid-prose can't cancel the session.
    texts = _slash_texts("fix this /e")
    assert "/explain-code" in texts
    assert "/exit" not in texts
    assert "/help" not in texts


def test_slash_completer_start_position_replaces_only_the_token() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    comp = prompt.JackCompleter({"/exit": "e"}, cwd=".", skills=_SKILLS)
    line = "fix this /deep"
    doc = Document(line, cursor_position=len(line))
    comps = list(comp.get_completions(doc, None))
    assert comps and comps[0].text == "/deep-research"
    assert comps[0].start_position == -len("/deep")  # replaces the token, not the whole line


def test_slash_completer_tags_skills_in_meta() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    comp = prompt.JackCompleter({}, cwd=".", skills=_SKILLS)
    doc = Document("/deep", cursor_position=5)
    comp0 = next(iter(comp.get_completions(doc, None)))
    assert "skill" in comp0.display_meta_text.lower()


def test_slash_completer_shortens_long_skill_descriptions() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    long_desc = "word " * 80  # ~400 chars, multi-run
    comp = prompt.JackCompleter({}, cwd=".", skills=[("big", long_desc)])
    doc = Document("/big", cursor_position=4)
    comp0 = next(iter(comp.get_completions(doc, None)))
    assert len(comp0.display_meta_text) < 80  # capped to one short line


# --- inline ghost-text auto-suggestion ------------------------------------------------------


def _suggestion(line: str) -> str | None:
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document

    comp = prompt.JackCompleter({"/help": "h", "/exit": "e"}, cwd=".", skills=_SKILLS)
    doc = Document(line, cursor_position=len(line))
    sug = prompt.JackAutoSuggest(comp).get_suggestion(Buffer(), doc)
    return None if sug is None else sug.text


def test_autosuggest_ghost_completes_a_command() -> None:
    pytest.importorskip("prompt_toolkit")
    assert _suggestion("/he") == "lp"  # /he + "lp" -> /help


def test_autosuggest_ghost_completes_a_skill_midline() -> None:
    pytest.importorskip("prompt_toolkit")
    assert _suggestion("go /deep") == "-research"  # /deep + "-research" -> /deep-research


def test_autosuggest_never_ghosts_a_control_command_midline() -> None:
    pytest.importorskip("prompt_toolkit")
    # Mid-line, /exi has no skill match and control commands aren't offered -> no ghost.
    assert _suggestion("fix /exi") is None


def test_autosuggest_none_for_plain_text() -> None:
    pytest.importorskip("prompt_toolkit")
    assert _suggestion("hello there") is None


def test_autosuggest_completes_a_file_path(tmp_path: Path) -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document

    (tmp_path / "apples.py").write_text("x", encoding="utf-8")
    comp = prompt.JackCompleter({}, cwd=str(tmp_path))
    line = "see @ap"
    sug = prompt.JackAutoSuggest(comp).get_suggestion(
        Buffer(), Document(line, cursor_position=len(line))
    )
    assert sug is not None and sug.text == "ples.py"  # @ap + "ples.py" -> @apples.py
