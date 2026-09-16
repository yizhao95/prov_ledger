"""I9: the decision-provenance wording stays neutral — no blame words in the
package docstrings, the CLI help, the dashboard templates or the doc. The
identifier `evidence_level` is allowed; the Chinese UI says 来源等级."""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BANNED = ("追责", "甩锅", "防老板", "呈堂")
FILES = [*(REPO / "orchestrator-backend" / "orchestrator").glob("*.py"),
         *(REPO / "orchestrator-webapp" / "app" / "templates").glob("*.html"),
         REPO / "docs" / "decision-provenance.md",
         REPO / "skills" / "executing-plans" / "SKILL.md"]


def test_i9_no_banned_words_in_code_docs_templates_and_help():
    hits = []
    for f in FILES:
        text = f.read_text(encoding="utf-8")
        for w in BANNED:
            if w in text:
                hits.append((str(f.relative_to(REPO)), w))
    assert hits == [], hits
    for argv in (["--help"], ["note", "--help"], ["metrics", "--help"], ["reasons", "--help"]):
        r = subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True,
                           cwd=str(REPO / "orchestrator-backend"))
        assert r.returncode == 0 and not any(w in r.stdout for w in BANNED), argv


def test_chinese_ui_says_source_level_not_evidence():
    """Templates may reference the evidence_level identifier, but any Chinese
    label next to it must read 来源等级, never 证据等级."""
    for f in (REPO / "orchestrator-webapp" / "app" / "templates").glob("*.html"):
        assert "证据等级" not in f.read_text(encoding="utf-8"), f.name
    q = (REPO / "orchestrator-webapp" / "app" / "queries.py").read_text(encoding="utf-8")
    assert "来源等级" in q and "证据等级" not in q
    doc = (REPO / "docs" / "decision-provenance.md").read_text(encoding="utf-8")
    assert "来源等级" in doc and "证据等级" not in doc
    assert re.search(r"evidence_level", doc)          # the identifier is documented under its real name


def test_phase2_docs_and_snippets_are_present_and_stay_neutral():
    """DP phase 2 (Task 8): the progressive-loading rules, the headline answer
    shapes and the degraded mode are documented where an agent reads them, and
    every new surface (hook scripts, the AGENTS.md snippet, the session module,
    the phase-2 doc sections) keeps the neutral wording."""
    from orchestrator import why
    dp = (REPO / "docs" / "decision-provenance.md").read_text(encoding="utf-8")
    assert "## 8 · Phase 2" in dp and "8.2 · Degraded mode" in dp and "read_hit" in dp and "influence" in dp
    wp = (REPO / "skills" / "writing-plans" / "SKILL.md").read_text(encoding="utf-8")
    assert "headline_notes" in wp and "never blocks" in wp and "provledger why" in wp
    ep = (REPO / "skills" / "executing-plans" / "SKILL.md").read_text(encoding="utf-8")
    assert "headline-respond.sh" in ep and '"because"' in ep and "Shown is not read" in ep
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "provledger why" in readme and "headline" in readme
    fl = (REPO / "docs" / "FUTURE-LOG.md").read_text(encoding="utf-8")
    assert "| FL-049 | dp-phase0/1 | DONE" in fl and "FL-066" in fl
    surfaces = [*(REPO / "hooks").glob("*.sh"), REPO / "orchestrator-backend" / "orchestrator" / "session.py"]
    hits = [(str(f.relative_to(REPO)), w) for f in surfaces for w in BANNED if w in f.read_text(encoding="utf-8")]
    hits += [("AGENTS snippet", w) for w in (*BANNED, "证据等级") if w in why.AGENTS_SNIPPET]
    hits += [("docs/decision-provenance.md §8", w) for w in (*BANNED, "证据等级") if w in dp.split("## 8 · Phase 2", 1)[1]]
    assert hits == [], hits
    for argv in (["why", "--help"], ["headline", "--help"], ["export", "--help"], ["init", "--help"]):
        r = subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True, cwd=str(REPO / "orchestrator-backend"))
        assert r.returncode == 0 and not any(w in r.stdout for w in BANNED), argv
