from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = [
    "skills/writing-plans/scripts/publish-plan.sh",
    "skills/writing-plans/scripts/ledger-add.sh",
    "skills/executing-plans/scripts/append-log.sh",
    "skills/executing-plans/scripts/fail-step.sh",
    "skills/executing-plans/scripts/finish-plan.sh",
    "skills/executing-plans/scripts/run-step.sh",
    "skills/executing-plans/scripts/agent-review-close.sh",
    "skills/executing-plans/scripts/complete-step.sh",
    "skills/executing-plans/scripts/deviate.sh",
    "skills/executing-plans/scripts/record-skill.sh",
    "skills/executing-plans/scripts/start-step.sh",
]
UNIFIED = '${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}/bin/python'


def test_all_resolvers_prefer_unified_venv():
    for rel in SCRIPTS:
        text = (ROOT / rel).read_text()
        assert UNIFIED in text, f"{rel} does not reference the unified venv"
        # existing system fallback must remain
        assert 'command -v python3' in text, f"{rel} dropped its python3 fallback"
