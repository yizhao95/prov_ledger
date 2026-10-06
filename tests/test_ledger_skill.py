"""skills/ledger/SKILL.md — the shape of a /ledger answer (skill redesign, 2026-10-06).

Real /ledger sessions in the release check showed three failures the old wording
invited: a reply that wandered before it answered; the goal of the task that
changed a node presented as that node's reason; and sentences `ask submit` had
deleted shown again underneath the checked answer. Anthropic's guidance for
skills and prompts says what fixes each: lead with the outcome, give a template
and a few complete examples, say what to do and why rather than list NEVERs, and
check every claim against a tool result before reporting it.

The user's own example of the answer they want hands the next person the whole
situation: what the thing is, who asked for it and in what words, what was done,
and what to keep in mind now.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "ledger" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _flat() -> str:
    return " ".join(_text().split())


def test_the_answer_template_hands_over_the_whole_situation_in_order():
    t = _flat()
    parts = ["What it is, or what changed and why", "Where it came from: who asked, when, in their own words",
             "What was done about it, oldest first", "What to keep in mind now"]
    at = [t.find(p) for p in parts]
    assert -1 not in at, [p for p, i in zip(parts, at) if i == -1]
    assert at == sorted(at), "the answer comes first, then where it came from, what was done, and now"


def test_three_complete_examples_each_saying_why_it_is_right():
    t = _text()
    examples = re.findall(r"<example>(.*?)</example>", t, re.S)
    assert len(examples) == 3
    for e in examples:
        assert "Why this is right:" in e
    kinds = " ".join(examples)
    assert "No reason was recorded for" in kinds, "one example is the node nobody explained"
    assert "rejected" in kinds, "one example is the path tried and rejected"
    assert "in her words" in kinds or "in his words" in kinds or "in their words" in kinds, \
        "one example quotes who asked for it"


def test_the_examples_do_not_carry_the_release_checks_answers():
    """Shipped examples are generic, and a release check that finds its own
    answer key in the skill measures nothing."""
    examples = " ".join(re.findall(r"<example>(.*?)</example>", _text(), re.S)).lower()
    for word in ("discount", "orders feed", "rollup", "load_orders", "weekly_report", "list_price"):
        assert word not in examples, word


def test_every_sentence_is_checked_against_this_sessions_reads_before_submitting():
    t = _flat()
    assert "Before you submit, check each sentence" in t
    assert "Which read in this session printed what it says?" in t


def test_a_deleted_sentence_is_not_shown_again_and_may_be_resubmitted_once_with_an_id():
    t = _flat()
    assert "do not show them again in other words" in t
    assert "submit once more with that id" in t and "If it is deleted again, leave it out" in t


def test_the_tiers_are_defined_by_the_legend_not_here():
    t = _flat()
    assert "legend" in t.lower()
    for definition in ("`stated` = ", "stated = a person", "asserted = a reading"):
        assert definition not in t, "one definition, in the CLI legend"


def test_the_draft_is_written_with_the_write_tool():
    """A heredoc into mktemp is a compound shell command a user's permission
    settings stop on every time; the real sessions in the release check all hit it."""
    assert "with the Write tool" in _flat()


def test_identifiers_and_the_scope_line_are_copied_never_made():
    """The A/B run of the redesign (2026-10-06): a commit hash no read had printed,
    and a scope line summed from two reads into one no command had printed. A
    number rule alone did not cover either."""
    t = _flat()
    assert "every number and identifier" in t.lower()
    assert "a commit, a plan id, a file:line" in t
    assert "the scope line as each read printed it" in t.lower() and "never added together" in t


def test_the_answer_may_be_detailed_up_to_fifteen_sentences():
    """The user (2026-10-06): /ledger is for the user, so it may be detailed and
    meticulous; ask submit keeps fifteen sentences."""
    t = _flat()
    assert "At most 15 sentences" in t and "At most 8 sentences" not in t
    assert "may be detailed" in t
