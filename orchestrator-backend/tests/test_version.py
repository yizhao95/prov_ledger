"""One version string, everywhere it is read. A release changes this test and
the seven files it names (CHANGELOG.md gets the new entry), and nothing else:
orchestrator/__init__.py, orchestrator-backend/pyproject.toml, the analyzer's
store.py TOOL_VERSION, plugin.json, marketplace.json, scripts/pkg_smoke_test.py
and CHANGELOG.md."""
import json
import re
from pathlib import Path

from orchestrator import __version__

REPO = Path(__file__).resolve().parents[2]


def test_every_version_string_agrees():
    assert __version__ == "0.4.6"
    pyproject = (REPO / "orchestrator-backend" / "pyproject.toml").read_text()
    assert re.search(r'^version = "0\.4\.6"$', pyproject, re.M)
    store = (REPO / "skills" / "project-state-graph" / "scripts" / "analyzer" / "store.py").read_text()
    assert re.search(r'^TOOL_VERSION = "0\.4\.6"$', store, re.M)
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == "0.4.6"
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    assert [p.get("version") for p in market["plugins"]] == ["0.4.6"]
    smoke = (REPO / "scripts" / "pkg_smoke_test.py").read_text()
    assert re.search(r'__version__ == "0\.4\.6"', smoke), "the wheel smoke test asserts another version"
    assert set(re.findall(r"\b\d+\.\d+\.\d+\b", smoke)) == {"0.4.6"}, "a half-bumped pkg_smoke_test.py"
    changelog = (REPO / "CHANGELOG.md").read_text()
    assert changelog.startswith("# Changelog") and "## 0.3.0" in changelog and "## 0.1.0" in changelog
