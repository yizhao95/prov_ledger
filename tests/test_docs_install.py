from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_readme_documents_plugin_install():
    text = (ROOT / "README.md").read_text()
    assert "/plugin marketplace add yizhao95/prov_ledger" in text
    assert "/plugin install provledger@provledger" in text


def test_install_md_mentions_superpowers_dependency():
    text = (ROOT / "INSTALL.md").read_text().lower()
    assert "superpowers" in text
