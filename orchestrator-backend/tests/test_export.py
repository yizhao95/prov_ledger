"""export — the whitelisted bundle, and the rows code refuses to let out
(DP phase 3, Task 2; spec §8, E1 veto).

The load-bearing test is the grep: after a bundle is written, every personal
string this project holds is searched for across every file. The whitelist is
the design; this is what makes the design checkable, and it is the only shape
of test that would have caught a future column quietly widening the export.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import constraints, provenance as pv

try:
    from orchestrator import export
except ImportError:                       # RED: the module does not exist yet
    export = None

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

PERSONAL_WORDS = "he only agreed because his bonus depends on the Q3 number"
PERSONAL_STATEMENT = "the VP overruled the analysts on the rollup grain"
PERSONAL_RATIONALE = "keeping this off the record until the reorg is announced"
PERSONAL_LABEL = "1:1 with the VP about the reorg timing"
SHAREABLE_TEXT = "weekly grain, to match how finance reports"
SHAREABLE_RATIONALE = "the daily grain double-counted refunds"
SHAREABLE_LABEL = "re: rollup grain, finance thread"


@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "g.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_snapshot(c, 1, "nk_a", "pkg.m.load_orders"); ps.add_event(c, 1, 1, "node_added", "nk_a")
    ps.add_snapshot(c, 1, "nk_b", "pkg.m.clean"); ps.add_event(c, 1, 2, "node_added", "nk_b")
    c.commit(); c.close()
    return str(path)


@pytest.fixture
def ledger(conn):
    """One project holding both halves of the boundary, on purpose."""
    ids = {}
    ids["personal_utterance"] = pv.insert_utterance(
        conn, session_id="s1", project="proj", plan_id="P0", text=PERSONAL_WORDS,
        occurred_at="2026-09-01 10:00:00", visibility="personal")
    ids["shareable_utterance"] = pv.insert_utterance(
        conn, session_id="s1", project="proj", plan_id="P0", text=SHAREABLE_TEXT,
        occurred_at="2026-09-01 10:05:00", visibility="shareable")
    ids["open_ref"] = pv.insert_reference(conn, project="proj", kind="email", label=SHAREABLE_LABEL,
                                          uri="mail:7", occurred_at="2026-09-01 09:00:00")
    ids["closed_ref"] = pv.insert_reference(conn, project="proj", kind="meeting", label=PERSONAL_LABEL,
                                            occurred_at="2026-09-01 08:00:00", visibility="personal")
    # a quotation of shareable words — travels, quotation and all
    ids["quoted_ok"] = pv.insert_reason(
        conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical",
        verbatim=(ids["shareable_utterance"], 0, 12), refs=[ids["open_ref"]], recorded_by="human")
    # a quotation of personal words — the record travels, the words do not
    ids["quoted_personal"] = pv.insert_reason(
        conn, project="proj", plan_id="P0", node_key="nk_a", kind="organizational",
        verbatim=(ids["personal_utterance"], 0, 20), recorded_by="human")
    # a shareable statement with a personal rationale and a shareable one
    ids["personal_rationale"] = pv.insert_reason(
        conn, project="proj", plan_id="P0", node_key="nk_b", kind="organizational",
        interpretation="the grain was decided upstairs", rationale=PERSONAL_RATIONALE,
        rationale_visibility="personal", recorded_by="agent")
    ids["shareable_rationale"] = pv.insert_reason(
        conn, project="proj", plan_id="P0", node_key="nk_b", kind="technical",
        interpretation="weekly rollup", rationale=SHAREABLE_RATIONALE,
        rationale_visibility="shareable", recorded_by="agent")
    # a personal statement — refused entry outright
    ids["personal_statement"] = pv.insert_reason(
        conn, project="proj", plan_id="P0", node_key="nk_b", kind="organizational",
        statement=PERSONAL_STATEMENT, statement_visibility="personal", recorded_by="human")
    # a reason linked to a personal reference
    ids["linked_personal_ref"] = pv.insert_reason(
        conn, project="proj", plan_id="P0", node_key="nk_a", kind="organizational",
        interpretation="the timing came from upstairs", refs=[ids["closed_ref"]], recorded_by="agent")
    constraints.record_constraint(conn, project="proj", subjects=["nk_a"],
                                  statement="keep paid orders only", rationale="finance said so")
    conn.commit()
    return ids


@pytest.fixture
def repo_with_anchor(conn, ledger, tmp_path):
    """A git repo carrying one real anchor, written the same way a close writes it."""
    from orchestrator import integrity
    d = tmp_path / "repo"
    d.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=str(d), check=True, capture_output=True)
    (d / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=str(d), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=str(d), check=True, capture_output=True)
    note = integrity.anchor_heads(d, integrity.anchor_payload(conn, plan_id="P0"))
    return d, note


def _all_text(root) -> str:
    out = []
    for dirpath, _d, files in os.walk(root):
        for name in sorted(files):
            out.append(Path(dirpath, name).read_text(encoding="utf-8", errors="replace"))
    return "\n".join(out)


@pytest.mark.veto
def test_e1_personal_text_never_reaches_the_bundle(conn, ledger, graph, tmp_path):
    """E1 (veto): a full-text sweep of the bundle finds no personal string."""
    manifest = export.bundle(conn, "proj", str(tmp_path / "out"), psg_db_path=graph)
    root = manifest["dir"]
    text = _all_text(root)
    for secret in (PERSONAL_WORDS, PERSONAL_STATEMENT, PERSONAL_RATIONALE, PERSONAL_LABEL):
        assert secret not in text, f"personal text left the machine: {secret!r}"
    grep = subprocess.run(["grep", "-rl", PERSONAL_WORDS, root], capture_output=True, text=True)
    assert grep.returncode == 1 and grep.stdout.strip() == ""
    # and the shareable half is genuinely there, so the test is not passing by emptiness
    assert SHAREABLE_TEXT[:12] in text and SHAREABLE_LABEL in text
    assert "keep paid orders only" in text


def test_utterance_is_never_a_table_in_the_bundle(conn, ledger, graph, tmp_path):
    manifest = export.bundle(conn, "proj", str(tmp_path / "out"), psg_db_path=graph)
    assert "utterance" in manifest["never_exported"]
    assert "utterance" not in manifest["whitelist"]
    lines = [json.loads(l) for l in Path(manifest["dir"], "records.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines and all(r["chain"] != "utterance" for r in lines)
    assert manifest["skipped"]["utterance"] == 2


def test_a_quotation_from_a_personal_utterance_is_withheld_but_counted(conn, ledger, graph, tmp_path):
    manifest = export.bundle(conn, "proj", str(tmp_path / "out"), psg_db_path=graph)
    lines = [json.loads(l) for l in Path(manifest["dir"], "records.jsonl").read_text(encoding="utf-8").splitlines()]
    withheld = [r for r in lines if r.get("id") == ledger["quoted_personal"] and r["chain"] == "change_reason"]
    assert len(withheld) == 1
    assert withheld[0]["text"] is None and "personal" in withheld[0]["quoted_withheld"]
    kept = [r for r in lines if r.get("id") == ledger["quoted_ok"] and r["chain"] == "change_reason"]
    assert kept[0]["text"] == SHAREABLE_TEXT[:12]
    assert manifest["skipped"]["personal_quote"] == 1
    assert manifest["skipped"]["personal_statement"] == 1
    assert manifest["skipped"]["personal_reference"] == 1


def test_include_rationale_is_per_record_and_lands_in_the_export_log(conn, ledger, graph, tmp_path):
    manifest = export.bundle(conn, "proj", str(tmp_path / "out"), psg_db_path=graph,
                             include_rationale=[ledger["shareable_rationale"]])
    text = _all_text(manifest["dir"])
    assert SHAREABLE_RATIONALE in text
    assert manifest["included_rationale"] == [ledger["shareable_rationale"]]
    row = conn.execute("SELECT project, fmt, included_rationale_json, exported_by FROM export_log "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "proj" and row[1] == "md"
    assert json.loads(row[2]) == [ledger["shareable_rationale"]]


def test_a_rationale_nobody_listed_stays_home_and_is_counted(conn, ledger, graph, tmp_path):
    manifest = export.bundle(conn, "proj", str(tmp_path / "out"), psg_db_path=graph)
    assert SHAREABLE_RATIONALE not in _all_text(manifest["dir"])
    assert manifest["skipped"]["rationale_not_requested"] >= 1
    assert manifest["included_rationale"] == []


@pytest.mark.veto
def test_include_rationale_refuses_a_personal_rationale(conn, ledger, graph, tmp_path):
    """E1 (veto): asking for a personal rationale by id is an error naming the
    id, not a quiet skip — and nothing is written."""
    out = tmp_path / "out"
    with pytest.raises(export.ExportViolation) as e:
        export.bundle(conn, "proj", str(out), psg_db_path=graph,
                      include_rationale=[ledger["personal_rationale"]])
    assert str(ledger["personal_rationale"]) in str(e.value) and "personal" in str(e.value)
    assert not (out / "proj").exists()
    assert conn.execute("SELECT COUNT(*) FROM export_log").fetchone()[0] == 0


def test_include_rationale_refuses_an_id_that_is_not_in_the_bundle(conn, ledger, graph, tmp_path):
    with pytest.raises(export.ExportViolation) as e:
        export.bundle(conn, "proj", str(tmp_path / "out"), psg_db_path=graph,
                      include_rationale=[ledger["personal_statement"]])
    assert str(ledger["personal_statement"]) in str(e.value)


def test_manifest_counts_match_the_records_file(conn, ledger, graph, tmp_path):
    manifest = export.bundle(conn, "proj", str(tmp_path / "out"), psg_db_path=graph)
    lines = [json.loads(l) for l in Path(manifest["dir"], "records.jsonl").read_text(encoding="utf-8").splitlines()]
    from collections import Counter
    seen = Counter(r["chain"] for r in lines)
    for chain, n in manifest["counts"].items():
        if chain == "records":
            assert n == len(lines)
        else:
            assert seen.get(chain, 0) == n, chain
    for entry in manifest["files"]:
        assert (Path(manifest["dir"]) / entry["path"]).exists()


def test_the_bundle_carries_the_chain_heads_and_says_when_there_is_no_anchor(conn, ledger, graph, tmp_path):
    from orchestrator import integrity
    manifest = export.bundle(conn, "proj", str(tmp_path / "out"), psg_db_path=graph)
    assert set(manifest["chain_heads"]) == set(integrity.CHAINS)
    for table in integrity.CHAINS:
        assert manifest["chain_heads"][table]["hash"] == conn.execute(
            f"SELECT hash FROM {table} ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert manifest["anchor"] is None and manifest["anchor_reason"]
    assert "not anchored" in Path(manifest["dir"], "README.md").read_text(encoding="utf-8")
    assert integrity.CLAIM in Path(manifest["dir"], "README.md").read_text(encoding="utf-8")


def test_the_bundle_quotes_the_anchor_when_the_repo_has_one(conn, ledger, graph, tmp_path, repo_with_anchor):
    repo, note = repo_with_anchor
    manifest = export.bundle(conn, "proj", str(tmp_path / "out"), psg_db_path=graph, repo=repo)
    assert manifest["anchor"]["note_sha"] == note
    assert note[:12] in Path(manifest["dir"], "README.md").read_text(encoding="utf-8")
    row = conn.execute("SELECT anchor_note_sha FROM export_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == note


def test_zip_roundtrip(conn, ledger, graph, tmp_path):
    import zipfile
    manifest = export.bundle(conn, "proj", str(tmp_path / "out"), psg_db_path=graph, fmt="zip")
    assert manifest["zip"].endswith("proj.zip") and os.path.exists(manifest["zip"])
    with zipfile.ZipFile(manifest["zip"]) as z:
        names = z.namelist()
        assert "proj/manifest.json" in names and "proj/README.md" in names
        assert any(n.startswith("proj/nodes/") for n in names)
        inner = json.loads(z.read("proj/manifest.json").decode("utf-8"))
        assert inner["counts"] == manifest["counts"] and inner["skipped"] == manifest["skipped"]
        assert z.read("proj/README.md").decode("utf-8") == Path(manifest["dir"], "README.md").read_text(encoding="utf-8")
        for secret in (PERSONAL_WORDS, PERSONAL_STATEMENT, PERSONAL_RATIONALE, PERSONAL_LABEL):
            assert all(secret not in z.read(n).decode("utf-8", "replace") for n in names)


def test_the_scan_deletes_the_bundle_when_personal_text_gets_through(conn, ledger, graph, tmp_path, monkeypatch):
    """If the whitelist ever springs a leak, the scan is what catches it: the
    bundle is removed and the caller is told which file held what."""
    original = export._pick

    def leaky(row, table):
        out = original(row, table)
        if table == "change_reason":
            out["rationale"] = dict(row).get("rationale")
        return out

    monkeypatch.setattr(export, "_pick", leaky)
    out = tmp_path / "out"
    with pytest.raises(export.ExportViolation) as e:
        export.bundle(conn, "proj", str(out), psg_db_path=graph)
    assert "personal text reached the bundle" in str(e.value)
    assert not (out / "proj").exists()


def test_cli_export_out_and_zip(conn, ledger, graph, tmp_path):
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    reg = tmp_path / "projects.json"
    reg.write_text('{"projects": [{"name": "proj", "repo": "/x", "db_path": "%s", "commit_sha": "c"}]}' % graph)
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"), PSG_REGISTRY_PATH=str(reg))
    r = subprocess.run([sys.executable, "-m", "orchestrator.cli", "export", "proj",
                        "--out", str(tmp_path / "b"), "--zip"],
                       capture_output=True, text=True, env=env, cwd=str(REPO))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["counts"]["change_reason"] >= 1 and out["zip"].endswith("proj.zip")
    assert (tmp_path / "b" / "proj" / "manifest.json").exists()
    assert PERSONAL_WORDS not in _all_text(tmp_path / "b")


def test_cli_export_md_still_works(conn, ledger, graph, tmp_path):
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    reg = tmp_path / "projects.json"
    reg.write_text('{"projects": [{"name": "proj", "repo": "/x", "db_path": "%s", "commit_sha": "c"}]}' % graph)
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"), PSG_REGISTRY_PATH=str(reg))
    r = subprocess.run([sys.executable, "-m", "orchestrator.cli", "export", "proj", "--md", str(tmp_path / "md")],
                       capture_output=True, text=True, env=env, cwd=str(REPO))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "md").is_dir()
