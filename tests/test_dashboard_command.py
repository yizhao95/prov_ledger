from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_launcher_uses_unified_venv_and_configurable_port():
    text = (ROOT / "orchestrator-webapp" / "launch_dashboard.sh").read_text()
    assert "PROVLEDGER_VENV" in text, "launcher not pointed at unified venv"
    assert "PROVLEDGER_DASH_PORT" in text, "launcher port not configurable"


def test_dashboard_command_exists_and_invokes_launcher():
    cmd = ROOT / "commands" / "provledger-dashboard.md"
    text = cmd.read_text()
    assert text.startswith("---"), "command file missing frontmatter"
    assert "launch_dashboard.sh" in text
    assert "CLAUDE_PLUGIN_ROOT" in text
