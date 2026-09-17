from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"

# These are always bundled — evolved or novel, never duplicates of superpowers.
ALWAYS_BUNDLED = [
    "writing-plans", "executing-plans",
    "project-state-graph", "update-project-state-graph",
    "ledger",
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
