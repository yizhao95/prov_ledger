import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_plugin_manifest_valid():
    data = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert data["name"] == "provledger"
    assert data["version"]
    assert data["description"]
    # skills are auto-discovered from skills/; manifest must not be empty
    assert "author" in data


def test_marketplace_lists_provledger():
    data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert data["name"] == "provledger"
    names = [p["name"] for p in data["plugins"]]
    assert "provledger" in names
    entry = next(p for p in data["plugins"] if p["name"] == "provledger")
    # plugin source is the marketplace root itself
    assert entry["source"] == "./"


def test_requirements_has_all_runtime_deps():
    reqs = (ROOT / "requirements.txt").read_text().lower()
    for pkg in ["pytest", "fastapi", "uvicorn", "jinja2",
                "tree-sitter", "tree-sitter-css", "tree-sitter-html",
                "tree-sitter-javascript", "tree-sitter-python"]:
        assert pkg in reqs, f"{pkg} missing from requirements.txt"
