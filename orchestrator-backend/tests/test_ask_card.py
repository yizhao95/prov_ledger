"""ask.card — the evidence card (DP phase 2e, Task 3; spec §21, J5).

The card says what a card can honestly say: these records existed at export
time and their chains still verify. It never says they are true.
"""
import json
import re
import sys
from pathlib import Path

import pytest

from orchestrator import ask, cli, provenance as pv
from orchestrator.ask import card as C

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

QUESTION = "why does build_features drop null labels?"


@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_snapshot(c, 1, "nk_p", "pkg.pipe.build_features")
    ps.add_event(c, 1, 1, "node_added", "nk_p", created_at="2026-09-01T09:00:00+00:00")
    c.commit(); c.close()
    return str(path)


@pytest.fixture
def seeded(conn, graph):
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0",
                            text="build_features must drop rows with a null label", occurred_at="2026-09-01 10:00:00")
    ref = pv.insert_reference(conn, project="proj", kind="meeting", label="feature review",
                              occurred_at="2026-09-01 09:30:00")
    cid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_p", kind="organizational", role="constraint",
                           verbatim=(u, 0, 46), refs=[ref], recorded_by="human", occurred_at="2026-09-01 10:00:00")
    conn.commit()
    return {"utterance": u, "reference": ref, "constraint": cid}


def _doc(conn, graph, answer=None):
    runner = None if answer is None else (lambda p, *, model=None, timeout_s=None: answer)
    chooser = (lambda p, *, model=None, timeout_s=None: json.dumps({"chosen": ["pkg.pipe.build_features"], "basis": "named"}))
    return ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph,
                   runner=chooser if answer is not None else None, summary_runner=runner,
                   model=None if answer is None else "stub-model")


def test_card_carries_question_answer_timeline_scope_and_the_ask_id(conn, graph, seeded):
    cid = seeded["constraint"]
    doc = _doc(conn, graph, answer=f"A constraint requires the null-label drop [#{cid}].")
    md = C.card_md(conn, doc)
    assert md.startswith("# Evidence card")
    assert QUESTION in md and f"[#{cid}]" in md
    assert f"ask id {doc['ask_id']}" in md and doc["facts_sha"][:16] in md
    assert doc["scope_line"] in md
    assert "stub-model" in md and "exported at" in md
    assert "not anchored" in md, "with no repository to read notes from, the card says so instead of implying a witness"
    assert "does not show that they are true" in md
    row = [ln for ln in md.splitlines() if f"| #{cid} " in ln]
    assert row and "2026-09-01" in row[0]
    chain_hash = conn.execute("SELECT hash FROM change_reason WHERE id = ?", (cid,)).fetchone()[0]
    assert chain_hash[:12] in md, "every record on the timeline shows its hash"


def test_j5_a_tampered_row_shows_a_broken_chain_at_that_record(conn, graph, seeded):
    cid = seeded["constraint"]
    doc = _doc(conn, graph, answer=f"A constraint requires the null-label drop [#{cid}].")
    assert "chain broken" not in C.card_md(conn, doc)
    conn.execute("DROP TRIGGER trg_change_reason_update_whitelist")     # someone edited the file directly
    conn.execute("UPDATE change_reason SET statement = 'rewritten' WHERE id = ?", (cid,))
    conn.commit()
    md = C.card_md(conn, doc)
    assert f"chain broken at #{cid}" in md
    integ = C.integrity(conn)
    assert integ["ok"] is False and integ["change_reason"]["first_bad_id"] == cid
    assert integ["utterance"]["ok"] is True


def test_the_card_exports_without_a_model(conn, graph, seeded):
    doc = _doc(conn, graph, answer=None)
    md = C.card_md(conn, doc)
    assert doc["degraded"] is True
    assert "summary unavailable: no model" in md and "Fact table" in md
    assert doc["scope_line"] in md and f"ask id {doc['ask_id']}" in md


def test_absences_and_dropped_counts_are_on_the_card(conn, graph, seeded):
    cid = seeded["constraint"]
    doc = _doc(conn, graph, answer=f"A constraint requires it [#{cid}]. This sentence guesses.")
    md = C.card_md(conn, doc)
    assert "1 sentence(s) dropped" in md
    assert any(a["text"] in md for a in doc["absences"])


def test_card_html_is_the_same_facts_escaped(conn, graph, seeded):
    cid = seeded["constraint"]
    doc = _doc(conn, graph, answer=f"A constraint requires the null-label drop [#{cid}].")
    html = C.card_html(conn, doc)
    assert html.lstrip().startswith("<!doctype html>") and "</html>" in html
    assert "<table" in html and f"#{cid}" in html and doc["scope_line"] in html
    assert "<script" not in html.lower()
    assert re.search(r"<title>Evidence card . ask \d+</title>", html)


def test_cli_export_writes_the_card(conn, graph, seeded, tmp_path, capsys, monkeypatch):
    import shutil
    conn.commit()
    db_path = tmp_path / "orch.db"
    shutil.copy(conn.execute("PRAGMA database_list").fetchone()[2], db_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": [{"name": "proj", "db_path": graph, "repo": str(tmp_path)}]}))
    monkeypatch.setenv("ORCH_DB", str(db_path))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(registry))
    out_file = tmp_path / "card.md"
    assert cli.main(["ask", QUESTION, "--project", "proj", "--no-model", "--export", str(out_file)]) == 0
    text = out_file.read_text(encoding="utf-8")
    assert text.startswith("# Evidence card") and "Scope:" in text
    assert str(out_file) in capsys.readouterr().out


# ── DP phase 3 (Task 3): the card quotes the anchor, or says there is none ────

def _git_repo(tmp_path):
    import subprocess
    d = tmp_path / "anchor-repo"
    d.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=str(d), check=True, capture_output=True)
    (d / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=str(d), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=str(d), check=True, capture_output=True)
    return d


def test_the_card_quotes_the_anchor_the_close_wrote(conn, graph, seeded, tmp_path):
    from orchestrator import integrity as I
    repo = _git_repo(tmp_path)
    note = I.anchor_heads(repo, I.anchor_payload(conn, plan_id="P0"))
    commit = I.head_commit(repo)
    doc = _doc(conn, graph, answer=f"A constraint requires the null-label drop [#{seeded['constraint']}].")
    md = C.card_md(conn, doc, repo=repo)
    assert f"git note {note[:12]} @ {commit[:12]}" in md
    assert "not anchored" not in md
    assert "plan P0" in md
    html = C.card_html(conn, doc, repo=repo)
    assert note[:12] in html and commit[:12] in html
    integ = C.integrity(conn, repo=repo)
    assert integ["anchor"]["note_sha"] == note and integ["ok"] is True


def test_the_card_says_not_anchored_and_why_when_the_repo_has_no_note(conn, graph, seeded, tmp_path):
    repo = _git_repo(tmp_path)
    doc = _doc(conn, graph, answer=f"A constraint requires the null-label drop [#{seeded['constraint']}].")
    md = C.card_md(conn, doc, repo=repo)
    assert "not anchored" in md and "refs/notes/provledger" in md
    assert C.integrity(conn, repo=repo)["anchor"] is None


def test_an_anchor_that_no_longer_describes_this_ledger_is_named_on_the_card(conn, graph, seeded, tmp_path):
    """The chains can walk and the witness can still disagree. That is a third
    state, and the card must not round it down to `ok`."""
    from orchestrator import integrity as I
    repo = _git_repo(tmp_path)
    payload = I.anchor_payload(conn, plan_id="P0")
    payload["change_reason"]["hash"] = "0" * 64
    I.anchor_heads(repo, payload)
    md = C.card_md(conn, doc := _doc(conn, graph, answer=f"Constraint [#{seeded['constraint']}]."), repo=repo)
    assert "anchor mismatch" in md
    assert C.integrity(conn, repo=repo)["ok"] is False
