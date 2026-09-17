"""integrity — the three chains and the git anchors they must agree with
(DP phase 3, Task 0; spec §7, G1/G2).

What these tests pin down:
  - the report names every chain's head, so a card or a note has something to quote;
  - a tampered row is NAMED, not swallowed (G1, veto);
  - a ledger with no anchor is still `ok` but is NOT `anchored`, and says why —
    the absence of an external witness is a fact, not silence;
  - the note is append-only: a second anchor never rewrites the first.
"""
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from orchestrator import provenance as pv

try:
    from orchestrator import integrity
except ImportError:                       # RED: the module does not exist yet
    integrity = None


# ── fixtures ──────────────────────────────────────────────────────────────────

def _seed(conn, n=2):
    """n utterances, n references and 2n reasons — every chain non-empty."""
    ids = {"utterance": [], "reference": [], "change_reason": []}
    for i in range(n):
        u = pv.insert_utterance(conn, session_id="s1", project="proj", plan_id="P0",
                                text=f"we keep the weekly grain, number {i}", occurred_at="2026-09-17 10:00:00")
        ids["utterance"].append(u)
        r = pv.insert_reference(conn, project="proj", kind="email", label=f"re: grain {i}",
                                uri=f"mail:{i}", occurred_at="2026-09-17 09:00:00")
        ids["reference"].append(r)
        ids["change_reason"].append(pv.insert_reason(
            conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical",
            verbatim=(u, 0, 10), refs=[r], recorded_by="human"))
        ids["change_reason"].append(pv.insert_reason(
            conn, project="proj", plan_id="P0", node_key="nk_b", kind="technical",
            interpretation=f"weekly grain matches finance ({i})", recorded_by="agent"))
    return ids


def _tampered_copy(conn, tmp_path, reason_id):
    """A copy of the ledger with the triggers dropped and one statement rewritten —
    exactly what someone editing the file behind the store's back would leave."""
    src = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.commit()
    dst = tmp_path / "tampered.db"
    shutil.copy(src, dst)
    c = sqlite3.connect(dst)
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='change_reason'").fetchall():
        c.execute(f"DROP TRIGGER {name}")
    c.execute("UPDATE change_reason SET interpretation = 'quietly rewritten' WHERE id = ?", (reason_id,))
    c.commit()
    c.close()
    out = sqlite3.connect(dst)
    out.row_factory = sqlite3.Row
    return out


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "t")
    (d / "a.txt").write_text("one\n")
    _git(d, "add", "a.txt")
    _git(d, "commit", "-qm", "one")
    return d


# ── the three chains ──────────────────────────────────────────────────────────

def test_g1_verify_reports_every_chain_head(conn):
    ids = _seed(conn, n=2)
    rep = integrity.verify(conn)
    assert rep["ok"] is True
    assert set(rep["tables"]) == set(integrity.CHAINS)
    for table in integrity.CHAINS:
        t = rep["tables"][table]
        assert t["ok"] is True and t["first_bad_id"] is None
        assert t["rows"] == len(ids[table])
        assert t["head_id"] == ids[table][-1]
        stored = conn.execute(f"SELECT hash FROM {table} WHERE id = ?", (t["head_id"],)).fetchone()[0]
        assert t["head_hash"] == stored
    # no --against-notes: the anchor question was not asked, and the report says so
    assert rep["anchored"] is False and rep["anchors"]["found"] == 0


@pytest.mark.veto
def test_g1_tampered_row_is_named(conn, tmp_path):
    """G1 (veto): edit a row behind the store's back and verify names that row."""
    ids = _seed(conn, n=2)
    target = ids["change_reason"][1]
    bad = _tampered_copy(conn, tmp_path, target)
    try:
        rep = integrity.verify(bad)
        assert rep["ok"] is False
        assert rep["tables"]["change_reason"]["ok"] is False
        assert rep["tables"]["change_reason"]["first_bad_id"] == target
        # the untouched chains are still fine — the report is per chain, not a single bit
        assert rep["tables"]["utterance"]["ok"] is True and rep["tables"]["reference"]["ok"] is True
    finally:
        bad.close()


def test_chain_heads_is_the_only_thing_an_anchor_needs(conn):
    ids = _seed(conn, n=1)
    heads = integrity.chain_heads(conn)
    assert set(heads) == set(integrity.CHAINS)
    for table in integrity.CHAINS:
        assert heads[table]["id"] == ids[table][-1]
        assert len(heads[table]["hash"]) == 64


def test_chain_heads_on_an_empty_ledger_say_none_not_zero(conn):
    heads = integrity.chain_heads(conn)
    for table in integrity.CHAINS:
        assert heads[table] == {"id": None, "hash": None, "rows": 0}


# ── anchors ───────────────────────────────────────────────────────────────────

def test_anchor_payload_carries_the_three_heads(conn):
    _seed(conn, n=1)
    payload = integrity.anchor_payload(conn, plan_id="P0")
    assert set(payload) == {"utterance", "reference", "change_reason", "at", "plan_id", "v"}
    assert payload["plan_id"] == "P0" and payload["v"] == 1
    assert payload["at"].endswith("Z")
    for table in integrity.CHAINS:
        assert set(payload[table]) == {"id", "hash"}


def test_anchor_and_read_roundtrip_appends_never_rewrites(conn, repo):
    _seed(conn, n=1)
    first = integrity.anchor_heads(repo, integrity.anchor_payload(conn, plan_id="P0"))
    assert first and len(first) == 40
    anchors = integrity.read_anchors(repo)
    assert len(anchors) == 1 and anchors[0]["plan_id"] == "P0"
    assert anchors[0]["commit"] == _git(repo, "rev-parse", "HEAD").strip()
    _seed(conn, n=1)
    second = integrity.anchor_heads(repo, integrity.anchor_payload(conn, plan_id="P1"))
    assert second != first
    anchors = integrity.read_anchors(repo)
    assert [a["plan_id"] for a in anchors] == ["P0", "P1"]          # the first one is still there
    assert anchors[0]["change_reason"]["id"] < anchors[1]["change_reason"]["id"]


def test_verify_against_notes_matches_the_prefix(conn, repo):
    ids = _seed(conn, n=1)
    integrity.anchor_heads(repo, integrity.anchor_payload(conn, plan_id="P0"))
    _seed(conn, n=2)                                                # the ledger grew after the anchor
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["ok"] is True and rep["anchored"] is True
    assert rep["anchors"]["found"] == 1 and rep["anchors"]["matched"] == 1
    assert rep["anchors"]["first_mismatch"] is None
    assert rep["anchors"]["latest"]["plan_id"] == "P0"
    assert rep["anchors"]["latest"]["note_sha"] and rep["anchors"]["latest"]["commit"]
    assert rep["tables"]["change_reason"]["head_id"] > ids["change_reason"][-1]


def test_verify_against_notes_names_the_first_mismatch(conn, repo):
    _seed(conn, n=1)
    payload = integrity.anchor_payload(conn, plan_id="P0")
    payload["change_reason"]["hash"] = "0" * 64                     # a note that no longer describes this ledger
    integrity.anchor_heads(repo, payload)
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    mm = rep["anchors"]["first_mismatch"]
    assert rep["anchors"]["found"] == 1 and rep["anchors"]["matched"] == 0
    assert mm["table"] == "change_reason" and mm["id"] == payload["change_reason"]["id"]
    assert mm["anchored_hash"] == "0" * 64 and mm["found_hash"] != mm["anchored_hash"]
    assert rep["ok"] is False                                       # the chains walk, but the witness disagrees


def test_verify_without_notes_is_ok_but_unanchored(conn, repo):
    """No anchor is a fact with a stated cause, never a silent zero."""
    _seed(conn, n=1)
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["ok"] is True and rep["anchored"] is False
    assert rep["anchors"]["found"] == 0 and rep["anchors"]["matched"] == 0
    assert "no note" in rep["anchors"]["reason"]


def test_verify_against_notes_without_a_repo_says_so(conn, tmp_path):
    _seed(conn, n=1)
    rep = integrity.verify(conn, against_notes=True, repo=tmp_path / "not-a-repo")
    assert rep["ok"] is True and rep["anchored"] is False
    assert rep["anchors"]["found"] == 0 and "git" in rep["anchors"]["reason"]


def test_anchor_heads_on_a_repo_without_head_raises_anchor_error(conn, tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    with pytest.raises(integrity.AnchorError) as e:
        integrity.anchor_heads(d, integrity.anchor_payload(conn, plan_id="P0"))
    assert "HEAD" in str(e.value)


def test_a_note_line_that_is_not_ours_is_counted_not_crashed_on(conn, repo):
    _seed(conn, n=1)
    integrity.anchor_heads(repo, integrity.anchor_payload(conn, plan_id="P0"))
    subprocess.run(["git", "notes", "--ref", "provledger", "append", "HEAD", "-m", "hand-written note"],
                   cwd=str(repo), capture_output=True, text=True, check=True)
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["anchors"]["found"] == 1 and rep["anchors"]["unreadable"] == 1
    assert rep["ok"] is True


def test_the_payload_is_one_line_so_append_stays_parseable(conn):
    _seed(conn, n=1)
    line = integrity.payload_line(integrity.anchor_payload(conn, plan_id="P0"))
    assert "\n" not in line
    assert json.loads(line)["plan_id"] == "P0"
