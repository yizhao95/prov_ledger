"""`provledger export --md` and `init --agents-md` (DP phase 2, Task 4; E1 first half)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import constraints, provenance as pv

try:
    from orchestrator import why
except ImportError:                       # RED: the module does not exist yet
    why = None

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "g.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_snapshot(c, 1, "nk_a", "pkg.m.load_orders"); ps.add_event(c, 1, 1, "node_added", "nk_a")
    ps.add_snapshot(c, 1, "nk_b", "pkg.m.clean"); ps.add_event(c, 1, 2, "node_added", "nk_b")
    c.commit(); c.close()
    return str(path)


def test_export_md_one_file_per_node_shareable_rows_only(conn, graph, tmp_path):
    c1 = constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="keep paid orders only", rationale="finance said so in the Q3 meeting", why_visibility="restricted")
    shared = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical", interpretation="weekly grain to match finance",
                              rationale="the boss prefers it", rationale_visibility="personal", recorded_by="agent")
    open_r = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_b", kind="technical", interpretation="clean drops nulls",
                              rationale="nulls broke the join", rationale_visibility="shareable", recorded_by="agent")
    secret = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_b", kind="organizational", interpretation="TOPSECRET reorg reason",
                              statement_visibility="personal", recorded_by="human")
    ref = pv.insert_reference(conn, project="proj", kind="email", label="re: paid orders", uri="mail:42", occurred_at="2026-09-01 10:00:00")
    conn.execute("INSERT INTO reference_link (reason_id, reference_id) VALUES (?, ?)", (open_r, ref)); conn.commit()
    out = why.export_md(conn, project="proj", out_dir=str(tmp_path / "md"), psg_db_path=graph)
    assert out["nodes"] == 2 and len(out["files"]) == 2 and sorted(Path(f).name for f in out["files"]) == ["pkg.m.clean.md", "pkg.m.load_orders.md"]
    a = (tmp_path / "md" / "pkg.m.load_orders.md").read_text(encoding="utf-8")
    b = (tmp_path / "md" / "pkg.m.clean.md").read_text(encoding="utf-8")
    assert "keep paid orders only" in a and "weekly grain to match finance" in a
    assert "the boss prefers it" not in a and "finance said so" not in a                   # personal rationale never leaves
    assert "nulls broke the join" in b and "re: paid orders" in b and "mail:42" in b        # shareable rationale + linked source
    assert "TOPSECRET" not in b and "TOPSECRET" not in a                                    # a personal statement is not exported at all
    assert "source level" in a and "history: 1 event(s)" in a
    grep = subprocess.run(["grep", "-rl", "keep paid orders only", str(tmp_path / "md")], capture_output=True, text=True)
    assert grep.stdout.strip().endswith("pkg.m.load_orders.md")


def test_e1_export_md_does_not_quote_a_personal_utterance(conn, graph, tmp_path):
    """DP phase 3: the older `--md` path quoted a verbatim span straight out of
    `utterance` without asking whether those words were personal. A shareable
    record may be built on words that are not shareable — the record travels,
    the quotation does not."""
    secret = "he only agreed because his bonus depends on the Q3 number"
    private = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0", text=secret,
                                  occurred_at="2026-09-01 10:00:00", visibility="personal")
    public = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0",
                                 text="weekly grain, to match how finance reports",
                                 occurred_at="2026-09-01 10:05:00", visibility="shareable")
    pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="organizational",
                     verbatim=(private, 0, len(secret)), recorded_by="human")
    pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical",
                     verbatim=(public, 0, 12), recorded_by="human")
    out = why.export_md(conn, project="proj", out_dir=str(tmp_path / "md"), psg_db_path=graph)
    text = "\n".join(Path(f).read_text(encoding="utf-8") for f in out["files"])
    assert secret not in text
    assert "weekly grain" in text                      # the shareable quotation still travels
    assert f"utterance #{private}" in text             # and the withholding is stated, not silent


def test_init_agents_md_is_idempotent(tmp_path):
    r1 = why.init_agents_md(str(tmp_path))
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert r1["action"] == "created" and "provledger why" in text and "provledger note" in text and "headline-respond" in text
    (tmp_path / "AGENTS.md").write_text("# mine\n\nkeep this\n" + text)
    r2 = why.init_agents_md(str(tmp_path))
    text2 = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert r2["action"] == "replaced" and text2.count("provledger:begin") == 1 and text2.startswith("# mine")
    (tmp_path / "AGENTS.md").write_text("# other\n")
    assert why.init_agents_md(str(tmp_path))["action"] == "appended"
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8").startswith("# other\n\n<!-- provledger:begin -->")
    for w in ("blame", "scapegoat", "pass the buck", "for the boss", "evidence level"):
        assert w not in why.AGENTS_SNIPPET


def test_cli_export_and_init(conn, graph, tmp_path):
    constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="keep paid orders only")
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    reg = tmp_path / "projects.json"
    reg.write_text('{"projects": [{"name": "proj", "repo": "/x", "db_path": "%s", "commit_sha": "c"}]}' % graph)
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"), PSG_REGISTRY_PATH=str(reg))
    r = subprocess.run([sys.executable, "-m", "orchestrator.cli", "export", "proj", "--md", str(tmp_path / "out")], capture_output=True, text=True, env=env, cwd=str(REPO))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "out" / "pkg.m.load_orders.md").exists()
    cwd = tmp_path / "repo"; cwd.mkdir()
    r = subprocess.run([sys.executable, "-m", "orchestrator.cli", "init", "--agents-md"], capture_output=True, text=True, env=env, cwd=str(cwd))
    assert r.returncode == 0, r.stderr
    assert (cwd / "AGENTS.md").exists() and "created" in r.stdout
