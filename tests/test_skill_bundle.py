from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"

# These are always bundled — evolved or novel, never duplicates of superpowers.
ALWAYS_BUNDLED = [
    "writing-plans", "executing-plans",
    "project-state-graph", "update-project-state-graph",
    # the two named question surfaces (spec §8.5): `/ledger` is the user's own
    # question about the project's history, `/receipts` helps them answer a
    # colleague. Both read-only, both grounded only in recorded rows. `receipts`
    # shipped as a CLI subcommand first and had no skill for a release; naming it
    # here is what keeps the pair from drifting apart again.
    "ledger", "receipts",
]

# The two commands the ledger skill is allowed to run. `/ledger` is a read; a
# read that can edit is not a read, and the rule only holds if it is written
# down where the model reads it.
LEDGER_COMMANDS = ("provledger ask ", "provledger ask submit ")


def test_core_skills_are_bundled():
    for s in ALWAYS_BUNDLED:
        assert (SKILLS / s / "SKILL.md").exists(), f"{s} must stay bundled"


def test_dropped_duplicates_are_gone():
    # Read the recorded decision; every skill marked IDENTICAL must be absent.
    # The decision lives in docs/superpowers/ (local-only, .gitignored), so a
    # fresh clone has nothing to check against: skip, don't fail (FL-016).
    decision_path = ROOT / "docs/superpowers/specs/2026-06-25-skill-diff.md"
    if not decision_path.exists():
        pytest.skip("local-only decision file docs/superpowers/specs/2026-06-25-skill-diff.md not present")
    decision = decision_path.read_text()
    for block in decision.split("## ")[1:]:
        name = block.splitlines()[0].strip()
        if "RESULT: IDENTICAL" in block:
            assert not (SKILLS / name).exists(), f"{name} marked IDENTICAL but still bundled"


def test_the_ledger_skill_declares_itself_and_stays_read_only():
    text = (SKILLS / "ledger" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md missing frontmatter"
    head = text.split("---")[1]
    assert "name: ledger" in head
    assert "Ask the ledger why / whether we tried — read-only" in head
    assert "/ledger <question>" in head, "the slash-command trigger must be in the description"
    for cmd in LEDGER_COMMANDS:
        assert cmd in text, f"the skill must name the command it runs: {cmd}"
    assert "provledger ask card" in text and "provledger why" in text, "the two follow-up reads must be shown"
    # the rule that makes it safe has to be legible, not implied
    assert "never edits, creates or deletes" in text
    assert "Never answer from memory" in text

# The reads added in 0.4.2 (spec §10.3). `/ledger` keeps its `ask` + `ask submit`
# flow, but a skill that does not name these leaves the model unable to reach a
# record whole, a neighbour by name, or a failure inside a COMPLETED task.
LEDGER_NAVIGATION_READS = ("provledger graph ", "provledger why ", "provledger record ", "provledger plan ")


def test_the_ledger_skill_names_the_reads_added_in_0_4_2():
    text = (SKILLS / "ledger" / "SKILL.md").read_text(encoding="utf-8")
    for cmd in LEDGER_NAVIGATION_READS:
        assert cmd in text, f"/ledger must name the read it can navigate with: {cmd}"


def test_the_ledger_skill_says_a_completed_task_can_hide_failures():
    """Verified on the live ledger: plan `dp6-a-20260927063613` reads COMPLETED and
    holds three FAILED steps, one of whose logs carries the whole derivation of a
    constant that was later questioned. A model that reads the status and stops
    never reaches it."""
    text = (SKILLS / "ledger" / "SKILL.md").read_text(encoding="utf-8")
    low = text.lower()
    assert "completed" in low and "recover" in low, \
        "/ledger must say that a recovered failure leaves the task's status clean"
    assert "provledger plan " in text


def test_the_ledger_skill_separates_what_was_searched_from_what_exists():
    """FL-158. In one session this absence was asserted three times, after three
    honest searches, while the answer sat in an unsearched column."""
    text = (SKILLS / "ledger" / "SKILL.md").read_text(encoding="utf-8")
    low = text.lower()
    assert "does not mean" in low or "not the same" in low, \
        "/ledger must state outright that finding nothing is not the same as nothing existing"

def test_the_ledger_skill_permits_the_repo_as_an_index():
    """The rule the user settled: provLedger **supplements** the code, it does not
    forbid reading it. Reading the repo is the agent's job and the normal way in —
    grep the constant, hand `file:line` to `why`, read the records, follow one to
    the task. The single failure to prevent is narrow: reading the code and
    **inventing a reason** for it, when the real reason is recorded in the task
    that made the change. An earlier wording banned source files outright and
    forbade step one."""
    text = (SKILLS / "ledger" / "SKILL.md").read_text(encoding="utf-8")
    low = text.lower()
    assert "no reading source files" not in low, \
        "a blanket ban on reading source files forbids the normal way in"
    assert "read the code freely" in low or "read the code" in low, \
        "the skill must say outright that the code is the agent's to read"
    assert "invent a reason" in low, \
        "and must name the one failure to prevent: a reason invented from the code"
    assert "file:line" in low, "and show how a code location becomes a node"

def test_the_ledger_skills_command_rule_does_not_cap_below_what_it_documents():
    text = (SKILLS / "ledger" / "SKILL.md").read_text(encoding="utf-8")
    assert "and no others" not in text, \
        "the skill documents six reads; a two-command cap contradicts them"
