"""0.2.0 release prep (phase 8 Task 5): one version string, everywhere it is read."""
import json
import re
from pathlib import Path

from orchestrator import __version__

REPO = Path(__file__).resolve().parents[2]


def test_every_version_string_agrees():
    assert __version__ == "0.2.0"
    pyproject = (REPO / "orchestrator-backend" / "pyproject.toml").read_text()
    assert re.search(r'^version = "0\.2\.0"$', pyproject, re.M)
    store = (REPO / "skills" / "project-state-graph" / "scripts" / "analyzer" / "store.py").read_text()
    assert re.search(r'^TOOL_VERSION = "0\.2\.0"$', store, re.M)
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == "0.2.0"
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    assert [p.get("version") for p in market["plugins"]] == ["0.2.0"]
    changelog = (REPO / "CHANGELOG.md").read_text()
    assert changelog.startswith("# Changelog") and "## 0.2.0" in changelog and "## 0.1.0" in changelog
