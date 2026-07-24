"""Slash-command parsing + dispatch."""

from __future__ import annotations

from autobot.cli.commands import (
    CommandResult,
    classify_line,
    dispatch,
    parse,
    skill_nudge,
)


def test_parse_splits_name_and_args() -> None:
    assert parse("/model gpt-4") == ("/model", "gpt-4")
    assert parse("/help") == ("/help", "")


def test_parse_non_command_is_none() -> None:
    assert parse("just a request") is None
    assert parse("") is None


def test_help_lists_commands() -> None:
    res = dispatch("/help", "")
    assert res.action == "message" and "/exit" in res.text and "/clear" in res.text


def test_help_lists_skills_when_given() -> None:
    res = dispatch("/help", "", [("brainstorming", "Explore ideas before building.")])
    assert "/brainstorming" in res.text and "Skills" in res.text
    assert "Explore ideas" in res.text


def test_help_omits_skill_section_without_skills() -> None:
    assert "Skills" not in dispatch("/help", "").text  # no skills → no empty section


def test_help_collapses_long_skill_description() -> None:
    res = dispatch("/help", "", [("x", "word " * 40)])  # ~200 chars, multi-space
    skill_row = next(line for line in res.text.splitlines() if line.strip().startswith("/x"))
    assert skill_row.endswith("…") and len(skill_row.strip()) <= 66  # "/x  " + 60-cap + ellipsis


def test_clear_and_exit_actions() -> None:
    assert dispatch("/clear", "") == CommandResult("clear")
    assert dispatch("/exit", "") == CommandResult("exit")


def test_unknown_command_hints() -> None:
    res = dispatch("/nope", "")
    assert res.action == "message" and "unknown" in res.text.lower()


def test_new_command_names_registered() -> None:
    from autobot.cli.commands import COMMANDS

    for name in ("/diff", "/undo", "/model", "/autonomy", "/sessions", "/new"):
        assert name in COMMANDS


def test_mcp_is_a_known_command() -> None:
    from autobot.cli.commands import COMMANDS

    assert "/mcp" in COMMANDS


# --- classify_line: command | skill | prose | unknown --------------------------------------

_SKILLS = frozenset({"deep-research", "explain-code"})


def test_classify_line_recognises_a_command() -> None:
    assert classify_line("/model gpt-4", _SKILLS) == ("command", "/model", "gpt-4")
    assert classify_line("/help", _SKILLS) == ("command", "/help", "")


def test_classify_line_recognises_a_skill() -> None:
    assert classify_line("/deep-research find X", _SKILLS) == (
        "skill",
        "deep-research",
        "find X",
    )
    assert classify_line("/explain-code", _SKILLS) == ("skill", "explain-code", "")


def test_classify_line_prose_is_not_a_directive() -> None:
    assert classify_line("just do the thing", _SKILLS) == ("prose", "", "")
    assert classify_line("", _SKILLS) == ("prose", "", "")


def test_classify_line_unknown_slash() -> None:
    assert classify_line("/nope", _SKILLS) == ("unknown", "/nope", "")


def test_classify_line_prefers_a_command_over_a_same_named_skill() -> None:
    # A built-in command wins if a skill happens to share its name (commands are the control
    # surface); the skill is still reachable by the model via the skill tool.
    assert classify_line("/help", frozenset({"help"})) == ("command", "/help", "")


def test_skill_nudge_names_the_skill_and_keeps_args() -> None:
    nudge = skill_nudge("deep-research", "find X")
    assert "deep-research" in nudge and "find X" in nudge


def test_skill_nudge_without_args_has_no_trailing_space() -> None:
    nudge = skill_nudge("explain-code", "")
    assert "explain-code" in nudge
    assert nudge == nudge.strip()
