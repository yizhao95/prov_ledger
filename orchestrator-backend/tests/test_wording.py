"""I9: the decision-provenance wording stays neutral — no blame words in the
package docstrings, the CLI help, the dashboard templates or the docs. The
identifier `evidence_level` is allowed; what a reader sees next to it is a
*source* level (来源等级 in the `?lang=zh` UI), never 证据等级. The Chinese
literals in this file are the words being searched for, not UI text."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BANNED = ("追责", "甩锅", "防老板", "呈堂")
KNOWN_ISSUES = REPO / "docs" / "KNOWN-ISSUES.md"
FILES = [*(REPO / "orchestrator-backend" / "orchestrator").glob("*.py"),
         *(REPO / "orchestrator-webapp" / "app" / "templates").glob("*.html"),
         REPO / "docs" / "decision-provenance.md",
         REPO / "skills" / "executing-plans" / "SKILL.md"]


def _known_issues() -> str:
    """The published limits doc. It is the user-facing half of the deferred-work
    ledger; the full ledger is kept out of the repository."""
    if not KNOWN_ISSUES.exists():
        pytest.skip("docs/KNOWN-ISSUES.md is not in this checkout")
    return KNOWN_ISSUES.read_text(encoding="utf-8")


def test_i9_no_banned_words_in_code_docs_templates_and_help():
    hits = []
    scanned = [*FILES, KNOWN_ISSUES] if KNOWN_ISSUES.exists() else FILES
    for f in scanned:
        text = f.read_text(encoding="utf-8")
        for w in BANNED:
            if w in text:
                hits.append((str(f.relative_to(REPO)), w))
    assert hits == [], hits
    for argv in (["--help"], ["note", "--help"], ["metrics", "--help"], ["reasons", "--help"]):
        r = subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True,
                           cwd=str(REPO / "orchestrator-backend"))
        assert r.returncode == 0 and not any(w in r.stdout for w in BANNED), argv


def test_the_source_level_label_never_reads_evidence_level():
    """`evidence_level` is the schema's column and it keeps its name. What a
    person reads beside it is a source level: 证据等级 ("evidence grade") was
    rejected because it invites a reader to treat a record as proof. The Chinese
    half of that label lives in the dashboard's vocabulary table and nowhere
    else."""
    vocab = (REPO / "orchestrator-webapp" / "app" / "vocab.py").read_text(encoding="utf-8")
    assert '"evidence_level"' in vocab and "可核对链接" in vocab      # the zh labels are still there
    assert "证据等级" not in vocab
    for f in (REPO / "orchestrator-webapp" / "app" / "templates").glob("*.html"):
        assert "证据等级" not in f.read_text(encoding="utf-8"), f.name
    doc = (REPO / "docs" / "decision-provenance.md").read_text(encoding="utf-8")
    assert "Source level" in doc and "证据等级" not in doc
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
    assert "| `why` |" in readme and "`headline show` / `respond` / `ack`" in readme
    ki = _known_issues()
    assert "read_hit" in ki and "--never-read" in ki          # phase 2's surfaces still have their limits on record
    surfaces = [*(REPO / "hooks").glob("*.sh"), REPO / "orchestrator-backend" / "orchestrator" / "session.py"]
    hits = [(str(f.relative_to(REPO)), w) for f in surfaces for w in BANNED if w in f.read_text(encoding="utf-8")]
    hits += [("AGENTS snippet", w) for w in (*BANNED, "证据等级") if w in why.AGENTS_SNIPPET]
    hits += [("docs/decision-provenance.md §8", w) for w in (*BANNED, "证据等级") if w in dp.split("## 8 · Phase 2", 1)[1]]
    assert hits == [], hits
    for argv in (["why", "--help"], ["headline", "--help"], ["export", "--help"], ["init", "--help"]):
        r = subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True, cwd=str(REPO / "orchestrator-backend"))
        assert r.returncode == 0 and not any(w in r.stdout for w in BANNED), argv


def test_phase2b_docs_and_the_new_views_stay_neutral():
    """DP phase 2b (Task 6): the three views, the triple and significance are documented;
    the new templates, queries and significance module keep the neutral wording."""
    from orchestrator import significance
    dp = (REPO / "docs" / "decision-provenance.md").read_text(encoding="utf-8")
    assert "## 9 · Phase 2b" in dp and "9.2 · Significance" in dp and "9.3 · R0" in dp and "(project, node, at)" in dp
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    # The three views are documented by what the section says, not by one image's alt text:
    # the images in section 2 are replaced from time to time, the claim is not.
    assert "one anchor, three angles" in readme and "The three share one anchor" in readme
    assert "`/session/{id}`" in readme
    ki = _known_issues()
    assert "The full graph view is slow" in ki               # the view's one known limit is still published
    surfaces = [REPO / "orchestrator-webapp" / "app" / "templates" / "graph.html", REPO / "orchestrator-webapp" / "app" / "templates" / "session.html",
                REPO / "orchestrator-backend" / "orchestrator" / "significance.py", REPO / "orchestrator-backend" / "orchestrator" / "testing" / "prompts" / "significance.md"]
    hits = [(str(f.relative_to(REPO)), w) for f in surfaces for w in (*BANNED, "证据等级") if w in f.read_text(encoding="utf-8")]
    hits += [("§9", w) for w in (*BANNED, "证据等级") if w in dp.split("## 9 · Phase 2b", 1)[1]]
    assert hits == [], hits
    for argv in (["reason", "--help"], ["significance", "--help"], ["significance", "eval", "--help"]):
        r = subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True, cwd=str(REPO / "orchestrator-backend"))
        assert r.returncode == 0 and not any(w in r.stdout for w in BANNED), argv
