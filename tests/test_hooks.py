import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_sessionstart_runs_bootstrap_async():
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    entries = data["hooks"]["SessionStart"]
    cmds = [h for e in entries for h in e["hooks"]]
    boot = [c for c in cmds if "bootstrap.sh" in c.get("command", "")]
    assert boot, "no SessionStart hook runs bootstrap.sh"
    assert all(c["type"] == "command" for c in boot)
    assert any(c.get("async") is True for c in boot), "bootstrap hook is not async"
    assert any("CLAUDE_PLUGIN_ROOT" in c["command"] for c in boot)
