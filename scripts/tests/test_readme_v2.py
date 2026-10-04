"""Tests for README.md — the v2 structure, its promises and its vocabulary.

The README is the product's first screen, so the things that make it honest
are asserted here rather than left to review:

  * the two hook lines are verbatim;
  * every image the page promises is referenced by the path it will live at;
  * sections 0-3 (everything a first-time reader sees) carry no internal
    vocabulary — the framework's rule is that those words appear from
    section 4 on, each with a one-line definition on first use;
  * the README stays a front page: install, a first run and pointers — the
    command reference lives in docs/cli.md (tested by test_cli_doc.py).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"

# The opening is where readers were leaving. What replaced it is a worked
# answer — a real question, real record ids, a counted search range — placed
# before any claim about what the tool is. These constants pin the parts of it
# that carry the argument, not its wording.
HOOK_LINES = (
    "Lost in a project's decision history? Watching your coding agent walk the same wrong path again?",
)
OPENING_CLAIM = "Your project, answering for itself"
SCOPE_MARKERS = ("Searched", "nothing left out")

IMAGES = (
    "docs/media/readme-hook.gif",
    "docs/media/readme-graph.gif",
    "docs/media/readme-task.png",
    "docs/media/readme-node.png",
    "docs/media/readme-state-graph.png",
    "docs/media/readme-ask.gif",
    "docs/media/readme-cli.gif",
)

# Internal vocabulary: precise where it belongs (the docs), noise on the
# first screen.
INTERNAL_WORDS = ("nk_", "asserted", "change_reason", "node_key", "plan_id")


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def intro(readme: str) -> str:
    """Sections 0-3: everything before the section 4 heading."""
    marker = "\n## 4 · "
    assert marker in readme, "README has no section 4 heading"
    return readme.split(marker, 1)[0]


def test_the_first_screen_answers_before_it_claims(readme: str, intro: str) -> None:
    """Readers were leaving at the first screen. A rhetorical question is where
    they leave: it asks them to agree they have a problem before they have been
    given anything. So the opening runs a real question through the tool and
    shows the answer, and only then says what the tool is."""
    assert readme.startswith("# provLedger\n"), "the title is the first line"
    first_screen = readme.split("\n## 1 · ", 1)[0]

    fence = first_screen.index("```")
    assert fence < first_screen.index(OPENING_CLAIM), "the answer comes before the claim about it"

    block = first_screen[fence:first_screen.index("```", fence + 3)]
    assert "/ledger " in block, "the opening shows the tool being used, not described"
    assert re.search(r"\[#\d+", block), "every claim in the answer carries a record id"
    assert re.search(r"\[r\d+", block), "the outside source is cited too, with its own id"
    for marker in SCOPE_MARKERS:
        assert marker in block, f"the answer must say how far it looked: {marker!r} missing"


def test_the_opening_questions_are_paid_off_immediately(readme: str) -> None:
    """The two questions are what makes a reader recognise themselves, so they
    stay. What they cannot do is stand alone: a question with nothing behind it
    is where the reader leaves. So the worked answer has to follow them on the
    first screen, before any claim about what the tool is."""
    first_screen = readme.split("\n## 1 · ", 1)[0]
    for line in HOOK_LINES:
        assert line in first_screen, f"opening line missing or reworded: {line!r}"

    last_question = max(first_screen.index(line) for line in HOOK_LINES)
    fence = first_screen.index("```")
    assert last_question < fence, "the questions come first, and the answer answers them"
    assert fence < first_screen.index(OPENING_CLAIM), "the answer comes before the claim about it"


@pytest.mark.parametrize("path", IMAGES)
def test_every_promised_image_is_referenced(readme: str, path: str) -> None:
    assert f"({path})" in readme, f"README does not reference {path}"


def test_the_hook_image_is_in_the_hook(intro: str) -> None:
    assert "(docs/media/readme-hook.gif)" in intro.split("\n## 1 · ", 1)[0]


@pytest.mark.parametrize("word", INTERNAL_WORDS)
def test_sections_0_to_3_carry_no_internal_vocabulary(intro: str, word: str) -> None:
    hits = [ln.strip() for ln in intro.splitlines() if word in ln]
    assert not hits, f"{word!r} appears before section 4:\n  " + "\n  ".join(hits)


def test_the_five_minute_section_shows_both_install_paths(readme: str) -> None:
    section = readme.split("\n## 3 · ", 1)[1].split("\n## 4 · ", 1)[0]
    assert "claude plugin marketplace add yizhao95/prov_ledger" in section
    assert "pip install provledger" in section


def test_the_reference_section_links_the_standing_documents(readme: str) -> None:
    for target in ("docs/KNOWN-ISSUES.md", "CHANGELOG.md", "LICENSE"):
        assert f"({target})" in readme, f"README does not link {target}"


def test_the_readme_is_a_front_page(readme: str) -> None:
    """Reference material has one home each, and it is not the README."""
    assert "\n### CLI\n" not in readme, "the command table lives in docs/cli.md"
    assert "(docs/cli.md)" in readme
    assert not re.search(r"\btests?-\d+", readme), "no hardcoded test count (run_tests.sh --count is the source)"
    assert len(readme.splitlines()) <= 260, "the README grew back into a manual"
