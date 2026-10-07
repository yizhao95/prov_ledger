"""integrity — the four chains and the git anchors they must agree with
(DP phase 3, Task 0; spec §7, G1/G2).

What these tests pin down:
  - the report names every chain's head, so a card or a note has something to quote;
  - `reference_check` is one of those chains: the record that says "I opened that
    pointer on that date" is exactly the one a doubter doubts, so it gets the same
    external witness as the other three;
  - a payload written by the previous version stays readable: an old note is
    evidence, and a version bump that made past evidence unreadable would be the
    opposite of the point;
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
from orchestrator.db import copy_ledger  # noqa: E402

try:
    from orchestrator import integrity
except ImportError:                       # RED: the module does not exist yet
    integrity = None


# ── fixtures ──────────────────────────────────────────────────────────────────

def _seed(conn, n=2, checks=True):
    """n utterances, n references, 2n reasons and (unless `checks=False`) n
    reference_check rows — every chain non-empty."""
    ids = {"utterance": [], "reference": [], "change_reason": [], "reference_check": []}
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
        if checks:
            ids["reference_check"].append(pv.insert_reference_check(
                conn, reference_id=r, verdict="ok", note=f"opened it, it was there ({i})"))
    return ids


def _tampered_copy(conn, tmp_path, row_id, *, table="change_reason", column="interpretation",
                   value="quietly rewritten", name="tampered.db"):
    """A copy of the ledger with the triggers dropped and one row rewritten —
    exactly what someone editing the file behind the store's back would leave."""
    src = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.commit()
    dst = tmp_path / name
    copy_ledger(src, dst)
    c = sqlite3.connect(dst)
    for (trg,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                            (table,)).fetchall():
        c.execute(f"DROP TRIGGER {trg}")
    c.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, row_id))
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


# ── the chains ────────────────────────────────────────────────────────────────

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

def test_anchor_payload_carries_every_chain_head(conn):
    """Was `..._the_three_heads`, v == 1. The check trail is a chain like the rest,
    so the payload names four heads and says v2 — strictly more than before.
    `plan_status` joined it as an addition to v2, not a v3: an older reader
    ignores a key it does not know."""
    ids = _seed(conn, n=1)
    payload = integrity.anchor_payload(conn, plan_id="P0")
    assert set(payload) == {"utterance", "reference", "change_reason", "reference_check",
                            "at", "plan_id", "plan_status", "v"}
    assert payload["plan_id"] == "P0" and payload["v"] == 2 == integrity.PAYLOAD_VERSION
    assert payload["at"].endswith("Z")
    for table in integrity.CHAINS:
        assert set(payload[table]) == {"id", "hash"}
        assert payload[table]["id"] == ids[table][-1]
        assert payload[table]["hash"] == conn.execute(
            f"SELECT hash FROM {table} WHERE id = ?", (ids[table][-1],)).fetchone()[0]


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
    _seed(conn, n=1)                      # something to anchor, so HEAD is the question being asked
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


# ── Task 1: a closed plan anchors the chain heads in git notes ────────────────

def _registry(tmp_path, name, repo, db_path="/g/missing.db"):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [
        {"name": name, "repo": str(repo), "db_path": db_path,
         "commit_sha": "abc", "updated_at": "2026-09-17T00:00:00+00:00"}]}))
    return str(p)


def _close(conn, plan_id, project, reg):
    from orchestrator import api, db as dbm
    dbm.insert_plan(conn, plan_id, f"Refactor the {project} pipeline")
    dbm.insert_step(conn, f"{plan_id}-A", plan_id, "CODE: work", 0, status="COMPLETED")
    review_id = dbm.insert_review_step(conn, plan_id)
    api.review_and_complete(conn, plan_id, registry_path=reg)
    api.start_step(conn, f"{review_id}.1")
    api.complete_step(conn, f"{review_id}.1")
    result = api.review_and_complete(conn, plan_id, registry_path=reg)
    return review_id, result


def test_a_closed_plan_anchors_the_three_chain_heads(conn, repo, tmp_path):
    from orchestrator import db as dbm
    ids = _seed(conn, n=2)
    reg = _registry(tmp_path, "demo-app", repo)
    review_id, result = _close(conn, "p-anchor", "demo-app", reg)
    assert result["plan_status"] == "COMPLETED"
    assert result["anchor"]["anchored"] is True
    anchors = integrity.read_anchors(repo)
    assert len(anchors) == 1
    a = anchors[0]
    assert a["plan_id"] == "p-anchor"
    for table in integrity.CHAINS:
        assert a[table]["id"] == ids[table][-1]
        assert a[table]["hash"] == conn.execute(
            f"SELECT hash FROM {table} WHERE id = ?", (ids[table][-1],)).fetchone()[0]
    log = dbm.get_step(conn, review_id)["log_context"]
    assert "[ANCHOR]" in log and a["note_sha"][:12] in log


def test_a_close_without_git_warns_and_never_blocks(conn, tmp_path):
    """spec §7: no git, no HEAD, or a notes failure is a logged warning — the
    plan still closes, and the log says what was lost."""
    from orchestrator import db as dbm
    _seed(conn, n=1)
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    reg = _registry(tmp_path, "demo-app", not_a_repo)
    review_id, result = _close(conn, "p-nogit", "demo-app", reg)
    assert result["plan_status"] == "COMPLETED"
    assert dbm.get_plan(conn, "p-nogit")["status"] == "COMPLETED"
    assert result["anchor"]["anchored"] is False and result["anchor"]["reason"]
    log = dbm.get_step(conn, review_id)["log_context"]
    assert "[ANCHOR] not anchored" in log


def test_anchor_off_writes_nothing_and_says_so(conn, repo, tmp_path):
    from orchestrator import db as dbm
    _seed(conn, n=1)
    (repo / "provledger-extensions.json").write_text(
        json.dumps({"version": 1, "integrity": {"anchor": "off"}}))
    reg = _registry(tmp_path, "demo-app", repo)
    review_id, result = _close(conn, "p-off", "demo-app", reg)
    assert result["plan_status"] == "COMPLETED"
    assert result["anchor"]["anchored"] is False and result["anchor"]["mode"] == "off"
    assert integrity.read_anchors(repo) == []
    assert "[ANCHOR] off" in dbm.get_step(conn, review_id)["log_context"]


def test_two_closes_leave_two_anchors_on_the_same_commit(conn, repo, tmp_path):
    _seed(conn, n=1)
    reg = _registry(tmp_path, "demo-app", repo)
    _close(conn, "p-one", "demo-app", reg)
    _seed(conn, n=1)
    _close(conn, "p-two", "demo-app", reg)
    anchors = integrity.read_anchors(repo)
    assert [a["plan_id"] for a in anchors] == ["p-one", "p-two"]
    assert anchors[0]["commit"] == anchors[1]["commit"]           # same HEAD, appended
    assert anchors[0]["change_reason"]["id"] < anchors[1]["change_reason"]["id"]


def test_the_anchor_a_close_wrote_is_what_verify_reads_back(conn, repo, tmp_path):
    _seed(conn, n=1)
    reg = _registry(tmp_path, "demo-app", repo)
    _close(conn, "p-round", "demo-app", reg)
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["ok"] is True and rep["anchored"] is True
    assert rep["anchors"]["found"] == 1 and rep["anchors"]["matched"] == 1
    assert rep["anchors"]["latest"]["plan_id"] == "p-round"


# ── Task 1 follow-up: an anchor must pin something, in the right repository ───

def test_a_close_with_nothing_to_anchor_writes_no_note(conn, repo, tmp_path):
    """An empty ledger has nothing for a witness to vouch for. A note full of
    nulls is not an anchor, it is noise on the ref."""
    from orchestrator import db as dbm
    reg = _registry(tmp_path, "demo-app", repo)
    review_id, result = _close(conn, "p-empty", "demo-app", reg)
    assert result["plan_status"] == "COMPLETED"
    assert result["anchor"]["anchored"] is False
    assert integrity.read_anchors(repo) == []
    assert _git(repo, "notes", "--ref", integrity.NOTES_REF, "list").strip() == ""
    assert "[ANCHOR] nothing to anchor" in dbm.get_step(conn, review_id)["log_context"]


def test_a_registered_path_inside_a_repo_is_refused(conn, repo, tmp_path):
    """The defect this test exists for: the phantom-uplift e2e suite registers
    `examples/phantom-uplift` — a directory INSIDE this repository — so every
    close in that test appended a note to the developer's own working repo. A
    path inside a work tree is not that work tree."""
    from orchestrator import db as dbm
    inner = repo / "examples" / "demo-app"
    inner.mkdir(parents=True)
    (inner / "x.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two")
    _seed(conn, n=1)
    reg = _registry(tmp_path, "demo-app", inner)
    review_id, result = _close(conn, "p-inner", "demo-app", reg)
    assert result["plan_status"] == "COMPLETED"                       # still never blocks
    assert result["anchor"]["anchored"] is False
    log = dbm.get_step(conn, review_id)["log_context"]
    assert "[ANCHOR] not anchored" in log and str(repo) in log        # names the root it would have landed in
    assert integrity.read_anchors(repo) == []
    assert _git(repo, "notes", "--ref", integrity.NOTES_REF, "list").strip() == ""


def test_anchor_heads_refuses_a_payload_that_pins_nothing(conn, repo):
    payload = integrity.anchor_payload(conn, plan_id="P0")            # empty ledger
    with pytest.raises(integrity.AnchorError) as e:
        integrity.anchor_heads(repo, payload)
    assert "nothing to anchor" in str(e.value)


def test_an_anchor_that_pins_nothing_is_not_counted_as_matched(conn, repo):
    """The ten notes the suite wrote are still on the ref of the repository that
    found this bug. They are counted as `empty`, not deleted and not silently
    passed off as witnesses — an anchor that pins no row vouches for nothing."""
    _seed(conn, n=1)
    integrity.anchor_heads(repo, integrity.anchor_payload(conn, plan_id="P0"))
    empty = {t: {"id": None, "hash": None} for t in integrity.CHAINS}
    empty.update({"at": "2026-09-17T06:38:53Z", "plan_id": "PU2", "v": 1})
    _git(repo, "notes", "--ref", integrity.NOTES_REF, "append", "HEAD", "-m", integrity.payload_line(empty))
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["anchors"]["found"] == 1 and rep["anchors"]["matched"] == 1
    assert rep["anchors"]["empty"] == 1
    assert rep["anchors"]["latest"]["plan_id"] == "P0"                # the empty one is never "latest"
    assert "1 line(s) on the ref pin nothing" in integrity.anchor_line(rep)


# ── the check trail gets the same external witness as everything else ─────────

def test_verify_walks_the_check_chain_too(conn):
    """Migration 030 hash-chains `reference_check`, and provenance.verify_chain
    accepts it. A chain nobody walks is a chain nobody would notice moving."""
    assert "reference_check" in integrity.CHAINS
    assert len(integrity.CHAINS) == 4
    ids = _seed(conn, n=2)
    rep = integrity.verify(conn)
    assert rep["ok"] is True
    t = rep["tables"]["reference_check"]
    assert t["ok"] is True and t["first_bad_id"] is None
    assert t["rows"] == len(ids["reference_check"]) == 2
    assert t["head_id"] == ids["reference_check"][-1]
    assert t["head_hash"] == conn.execute(
        "SELECT hash FROM reference_check WHERE id = ?", (t["head_id"],)).fetchone()[0]
    assert f"chain reference_check: ok" in integrity.render(rep)


@pytest.mark.veto
def test_g1_a_tampered_reference_check_row_is_named(conn, tmp_path):
    """The claim under doubt is "you say you opened that link". Rewrite what the
    checker wrote and verify must say which row, not just that something is off."""
    ids = _seed(conn, n=2)
    target = ids["reference_check"][0]
    bad = _tampered_copy(conn, tmp_path, target, table="reference_check", column="note",
                         value="it was there, honestly", name="tampered-check.db")
    try:
        rep = integrity.verify(bad)
        assert rep["ok"] is False
        assert rep["tables"]["reference_check"]["ok"] is False
        assert rep["tables"]["reference_check"]["first_bad_id"] == target
        for other in ("utterance", "reference", "change_reason"):
            assert rep["tables"][other]["ok"] is True
        assert f"chain reference_check: chain broken at #{target}" in integrity.render(rep)
    finally:
        bad.close()


def test_an_anchor_pins_the_check_chain_head_and_a_moved_check_is_a_mismatch(conn, repo):
    _seed(conn, n=1)
    payload = integrity.anchor_payload(conn, plan_id="P0")
    assert payload["reference_check"]["id"] is not None
    payload["reference_check"]["hash"] = "0" * 64      # a note that no longer describes this ledger
    integrity.anchor_heads(repo, payload)
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    mm = rep["anchors"]["first_mismatch"]
    assert rep["ok"] is False
    assert mm["table"] == "reference_check" and mm["id"] == payload["reference_check"]["id"]
    assert mm["anchored_hash"] == "0" * 64 and mm["found_hash"] != mm["anchored_hash"]
    assert "reference_check" in integrity.anchor_line(rep)


def test_an_empty_check_chain_anchors_as_null_not_zero(conn, repo):
    """A ledger with reasons but no checks yet: the check head is `null`, the same
    word chain_heads already uses for the other three. `0` would name row zero."""
    _seed(conn, n=1, checks=False)
    heads = integrity.chain_heads(conn)
    assert heads["reference_check"] == {"id": None, "hash": None, "rows": 0}
    payload = integrity.anchor_payload(conn, plan_id="P0")
    assert payload["reference_check"] == {"id": None, "hash": None}
    assert '"reference_check":{"hash":null,"id":null}' in integrity.payload_line(payload)
    # the other three still pin rows, so this is a real anchor and it still matches
    integrity.anchor_heads(repo, payload)
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["ok"] is True and rep["anchored"] is True
    assert rep["anchors"]["found"] == 1 and rep["anchors"]["matched"] == 1
    assert rep["anchors"]["latest"]["heads"]["reference_check"] == {"id": None, "hash": None}


def test_a_v1_payload_is_still_readable_after_the_version_bump(conn, repo):
    """An old note is evidence. A version bump that made past evidence unreadable
    would be the opposite of the point, so v1 lines are anchors, not `unreadable`."""
    ids = _seed(conn, n=1)
    v1 = {t: {"id": ids[t][-1],
              "hash": conn.execute(f"SELECT hash FROM {t} WHERE id = ?", (ids[t][-1],)).fetchone()[0]}
          for t in ("utterance", "reference", "change_reason")}
    v1.update({"at": "2026-09-17T06:38:53Z", "plan_id": "P-old", "v": 1})
    assert "reference_check" not in v1
    _git(repo, "notes", "--ref", integrity.NOTES_REF, "append", "HEAD", "-m", integrity.payload_line(v1))
    anchors = integrity.read_anchors(repo)
    assert len(anchors) == 1
    assert "unreadable" not in anchors[0] and not anchors[0].get("empty")
    assert anchors[0]["plan_id"] == "P-old" and anchors[0]["v"] == 1
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["ok"] is True and rep["anchored"] is True
    assert rep["anchors"]["found"] == 1 and rep["anchors"]["matched"] == 1
    assert rep["anchors"]["unreadable"] == 0 and rep["anchors"]["empty"] == 0
    assert rep["anchors"]["latest"]["heads"]["reference_check"] is None   # v1 pinned no check head


def test_a_v1_and_a_v2_anchor_sit_on_the_ref_together(conn, repo):
    """The realistic state of any repository that was anchored before this change."""
    ids = _seed(conn, n=1)
    v1 = {t: {"id": ids[t][-1],
              "hash": conn.execute(f"SELECT hash FROM {t} WHERE id = ?", (ids[t][-1],)).fetchone()[0]}
          for t in ("utterance", "reference", "change_reason")}
    v1.update({"at": "2026-09-17T06:38:53Z", "plan_id": "P-old", "v": 1})
    _git(repo, "notes", "--ref", integrity.NOTES_REF, "append", "HEAD", "-m", integrity.payload_line(v1))
    _seed(conn, n=1)
    integrity.anchor_heads(repo, integrity.anchor_payload(conn, plan_id="P-new"))
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["ok"] is True
    assert rep["anchors"]["found"] == 2 and rep["anchors"]["matched"] == 2
    assert rep["anchors"]["unreadable"] == 0
    assert rep["anchors"]["latest"]["plan_id"] == "P-new"
    assert [a["v"] for a in integrity.read_anchors(repo)] == [1, 2]


def test_a_future_payload_version_is_unreadable_not_trusted(conn, repo):
    """Forward compatibility is not a promise this module can keep: a payload from
    a version it does not know is counted, never guessed at."""
    _seed(conn, n=1)
    integrity.anchor_heads(repo, integrity.anchor_payload(conn, plan_id="P0"))
    ahead = integrity.anchor_payload(conn, plan_id="P-future")
    ahead["v"] = integrity.PAYLOAD_VERSION + 1
    _git(repo, "notes", "--ref", integrity.NOTES_REF, "append", "HEAD", "-m", integrity.payload_line(ahead))
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["anchors"]["found"] == 1 and rep["anchors"]["unreadable"] == 1
    assert rep["anchors"]["latest"]["plan_id"] == "P0"


def test_a_closed_plan_anchors_the_check_chain_head(conn, repo, tmp_path):
    """End to end: the witness a close writes now covers the check trail."""
    ids = _seed(conn, n=1)
    reg = _registry(tmp_path, "demo-app", repo)
    _close(conn, "p-check", "demo-app", reg)
    a = integrity.read_anchors(repo)[0]
    assert a["v"] == integrity.PAYLOAD_VERSION
    assert a["reference_check"]["id"] == ids["reference_check"][-1]
    assert a["reference_check"]["hash"] == conn.execute(
        "SELECT hash FROM reference_check WHERE id = ?", (ids["reference_check"][-1],)).fetchone()[0]


# ── the witness is not tied to success ────────────────────────────────────────
#
# The witness is not tied to success. A plan can fail because a gate did not pass,
# or because its
# own bookkeeping jammed, but the rows it recorded do not become untrue when it
# fails. What an anchor attests is "these rows existed at this commit and have
# not been altered since" — a claim about rows, not about how the work went.
# Anchoring only successes left the records most likely to be disputed, the ones
# from a run that went wrong, as the only ones with no outside witness.

def _close_failed(conn, plan_id, project, reg):
    """A plan that closes FAILED through an unrecovered regular step — the
    commonest failed close, and the one the FAILED path never intercepts."""
    from orchestrator import api, db as dbm
    dbm.insert_plan(conn, plan_id, f"Refactor the {project} pipeline")
    dbm.insert_step(conn, f"{plan_id}-A", plan_id, "CODE: work", 0)
    review_id = dbm.insert_review_step(conn, plan_id)
    api.start_step(conn, f"{plan_id}-A")
    api.fail_step(conn, f"{plan_id}-A", "the gate did not pass")
    result = api.review_and_complete(conn, plan_id, registry_path=reg)
    return review_id, result


def _close_review_failed(conn, plan_id, project, reg):
    """A plan whose agent review itself failed — the other FAILED close."""
    from orchestrator import api, db as dbm
    dbm.insert_plan(conn, plan_id, f"Refactor the {project} pipeline")
    dbm.insert_step(conn, f"{plan_id}-A", plan_id, "CODE: work", 0, status="COMPLETED")
    review_id = dbm.insert_review_step(conn, plan_id)
    api.review_and_complete(conn, plan_id, registry_path=reg)
    api.start_step(conn, f"{review_id}.1")
    api.fail_step(conn, f"{review_id}.1", "the review found the work unsound")
    result = api.review_and_complete(conn, plan_id, registry_path=reg)
    return review_id, result


def test_a_failed_plan_close_anchors_its_chain_heads(conn, repo, tmp_path):
    """The defect: `_anchor_close` sat inside the COMPLETED return, so 64
    change_reason rows sat in the live ledger with no external witness."""
    from orchestrator import db as dbm
    ids = _seed(conn, n=2)
    reg = _registry(tmp_path, "demo-app", repo)
    review_id, result = _close_failed(conn, "p-failed", "demo-app", reg)
    assert result["plan_status"] == "FAILED"
    assert result["anchor"]["anchored"] is True
    anchors = integrity.read_anchors(repo)
    assert len(anchors) == 1
    a = anchors[0]
    assert a["plan_id"] == "p-failed"
    for table in integrity.CHAINS:
        assert a[table]["id"] == ids[table][-1]
    log = dbm.get_step(conn, review_id)["log_context"]
    assert "[ANCHOR]" in log and a["note_sha"][:12] in log


def test_a_failed_agent_review_close_anchors_too(conn, repo, tmp_path):
    _seed(conn, n=1)
    reg = _registry(tmp_path, "demo-app", repo)
    _review_id, result = _close_review_failed(conn, "p-rev-failed", "demo-app", reg)
    assert result["plan_status"] == "FAILED"
    assert result["anchor"]["anchored"] is True
    anchors = integrity.read_anchors(repo)
    assert [a["plan_id"] for a in anchors] == ["p-rev-failed"]
    assert anchors[0]["plan_status"] == "FAILED"


def test_the_note_says_which_close_it_witnessed(conn, repo, tmp_path):
    """A witness that cannot tell you whether the run succeeded is a worse
    witness. The payload carries the value that was written."""
    _seed(conn, n=1)
    reg = _registry(tmp_path, "demo-app", repo)
    _close_failed(conn, "p-bad", "demo-app", reg)
    _seed(conn, n=1)
    _close(conn, "p-good", "demo-app", reg)
    anchors = integrity.read_anchors(repo)
    assert {a["plan_id"]: a["plan_status"] for a in anchors} == {
        "p-bad": "FAILED", "p-good": "COMPLETED"}


def test_verify_against_notes_matches_a_failed_plans_anchor_like_any_other(conn, repo, tmp_path):
    _seed(conn, n=1)
    reg = _registry(tmp_path, "demo-app", repo)
    _close_failed(conn, "p-failed-verify", "demo-app", reg)
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["ok"] is True and rep["anchored"] is True
    assert rep["anchors"]["found"] == 1 and rep["anchors"]["matched"] == 1
    assert rep["anchors"]["unreadable"] == 0
    assert rep["anchors"]["latest"]["plan_id"] == "p-failed-verify"


def test_anchor_off_suppresses_a_failed_close_too(conn, repo, tmp_path):
    from orchestrator import db as dbm
    _seed(conn, n=1)
    (repo / "provledger-extensions.json").write_text(
        json.dumps({"version": 1, "integrity": {"anchor": "off"}}))
    reg = _registry(tmp_path, "demo-app", repo)
    review_id, result = _close_failed(conn, "p-off-failed", "demo-app", reg)
    assert result["plan_status"] == "FAILED"
    assert result["anchor"]["anchored"] is False and result["anchor"]["mode"] == "off"
    assert integrity.read_anchors(repo) == []
    assert "[ANCHOR] off" in dbm.get_step(conn, review_id)["log_context"]


def test_an_anchor_failure_never_blocks_a_failed_close(conn, tmp_path):
    from orchestrator import db as dbm
    _seed(conn, n=1)
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    reg = _registry(tmp_path, "demo-app", not_a_repo)
    review_id, result = _close_failed(conn, "p-nogit-failed", "demo-app", reg)
    assert result["plan_status"] == "FAILED"
    assert dbm.get_plan(conn, "p-nogit-failed")["status"] == "FAILED"
    assert result["anchor"]["anchored"] is False and result["anchor"]["reason"]
    assert "[ANCHOR] not anchored" in dbm.get_step(conn, review_id)["log_context"]


def test_plan_status_is_an_addition_not_a_new_payload_version(conn, repo):
    """A new optional field is something older readers ignore. A v3 payload
    would be counted `unreadable` by the very code that wrote it, so the
    version stays where it is."""
    _seed(conn, n=1)
    payload = integrity.anchor_payload(conn, plan_id="P0", plan_status="FAILED")
    assert payload["v"] == 2 and integrity.PAYLOAD_VERSION == 2
    assert payload["plan_status"] == "FAILED"
    integrity.anchor_heads(repo, payload)
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["anchors"]["found"] == 1 and rep["anchors"]["matched"] == 1
    assert rep["anchors"]["unreadable"] == 0
    assert integrity.read_anchors(repo)[0]["plan_status"] == "FAILED"


def test_an_old_note_without_plan_status_is_still_read(conn, repo):
    """The field is optional both ways: the anchors already on the live ref were
    written before it existed and stay evidence."""
    _seed(conn, n=1)
    payload = integrity.anchor_payload(conn, plan_id="P-old")
    payload.pop("plan_status", None)
    integrity.anchor_heads(repo, payload)
    anchors = integrity.read_anchors(repo)
    assert len(anchors) == 1 and "unreadable" not in anchors[0]
    assert anchors[0].get("plan_status") is None
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["anchors"]["matched"] == 1


def test_the_report_says_how_the_latest_anchored_plan_closed(conn, repo, tmp_path):
    """The field is only useful if a reader can see it: `verify` carries it and
    the one human-readable line prints it."""
    _seed(conn, n=1)
    reg = _registry(tmp_path, "demo-app", repo)
    _close_failed(conn, "p-shown", "demo-app", reg)
    rep = integrity.verify(conn, against_notes=True, repo=repo)
    assert rep["anchors"]["latest"]["plan_status"] == "FAILED"
    assert "plan p-shown FAILED" in integrity.anchor_line(rep)
