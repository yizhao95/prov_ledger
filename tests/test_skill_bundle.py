from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"

# These are always bundled — evolved or novel, never duplicates of superpowers.
ALWAYS_BUNDLED = [
    "writing-plans", "executing-plans",
    "project-state-graph", "update-project-state-graph",
]


def test_core_skills_are_bundled():
    for s in ALWAYS_BUNDLED:
        assert (SKILLS / s / "SKILL.md").exists(), f"{s} must stay bundled"


def test_dropped_duplicates_are_gone():
    # Read the recorded decision; every skill marked IDENTICAL must be absent.
    decision = (ROOT / "docs/superpowers/specs/2026-06-25-skill-diff.md").read_text()
    for block in decision.split("## ")[1:]:
        name = block.splitlines()[0].strip()
        if "RESULT: IDENTICAL" in block:
            assert not (SKILLS / name).exists(), f"{name} marked IDENTICAL but still bundled"
