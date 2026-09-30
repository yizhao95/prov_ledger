from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_readme_documents_plugin_install():
    """Either spelling is correct — `claude plugin ...` from a shell, `/plugin ...`
    inside a session — so the assertion is that the README shows one of them, not
    that it shows the one this test happened to be written against."""
    text = (ROOT / "README.md").read_text()
    for verb in ("marketplace add yizhao95/prov_ledger", "install provledger@provledger"):
        assert f"claude plugin {verb}" in text or f"/plugin {verb}" in text, \
            f"README documents neither spelling of `plugin {verb}`"


def test_install_md_mentions_superpowers_dependency():
    text = (ROOT / "INSTALL.md").read_text().lower()
    assert "superpowers" in text
