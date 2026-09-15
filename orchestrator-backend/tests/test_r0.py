"""R0 — the user's own words naming a node become its stated reason (DP phase 2, Task 0).
Literal, case-sensitive, `_` intact; local names shorter than 4 chars or in the
stopword list never hit; a sentence naming more than 3 nodes is generic."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import provenance as pv, reasons, triggers

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

PLAN = "P1"
REPO = Path(__file__).resolve().parents[2]
NODES = {"nk_a": ("pkg.rollup.weekly_totals", "pkg/rollup.py"), "nk_b": ("pkg.m.run", "pkg/m.py"),
         "nk_c": ("pkg.m.clean_rows", "pkg/clean.py"), "nk_d": ("pkg.m.load_orders", "pkg/m.py"),
         "nk_e": ("pkg.m.save_all", "pkg/m.py")}


def _graph(tmp_path, keys=("nk_a", "nk_b", "nk_c", "nk_d", "nk_e")):
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_run(c, 2, plan_id=PLAN, step_id=f"{PLAN}-REVIEW.1")
    for i, k in enumerate(keys, 1):
        qn, fp = NODES[k]
        c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, dataflow_sig, dataflow_trivial, attrs_json) "
                  "VALUES (1, ?, 'function', ?, ?, 's1', NULL, 1, '{}')", (k, qn, fp))
        ps.add_event(c, 1, i, "node_added", k)
        c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, dataflow_sig, dataflow_trivial, attrs_json) "
                  "VALUES (2, ?, 'function', ?, ?, 's2', NULL, 1, '{}')", (k, qn, fp))
        ps.add_event(c, 2, i, "node_changed", k, '{"changed": ["struct_sig"]}')
    c.commit(); c.close()
    return str(path)


def _plan(conn, user_query=None):
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at, user_query) VALUES "
                 "(?, 'goal', 'IN_PROGRESS', 'proj', 'declared', '2026-09-15 10:00:00', ?)", (PLAN, user_query))
    conn.commit()


def _utt(conn, text, plan_id=PLAN, session="s7", project="proj", at="2026-09-15 10:05:00"):
    return pv.insert_utterance(conn, session_id=session, project=project, plan_id=plan_id, text=text, occurred_at=at)


def _eval(conn, graph):
    return triggers.evaluate(conn, project="proj", plan_id=PLAN, psg_db_path=graph, commit=True)


def _stated(conn):
    return {r["node_key"]: r for r in pv.reasons_for_plan(conn, PLAN) if r["tier"] == "stated"}


def _span_text(conn, r):
    t = conn.execute("SELECT text FROM utterance WHERE id=?", (r["verbatim_utterance_id"],)).fetchone()[0]
    return t[r["verbatim_start"]:r["verbatim_end"]]


def test_r0_local_name_hit_is_stated_with_the_sentence_as_span(conn, tmp_path):
    graph = _graph(tmp_path, keys=("nk_a",))
    _plan(conn)
    u = _utt(conn, "First a preface. please keep weekly_totals on fiscal weeks! not calendar weeks")
    r = _eval(conn, graph)
    assert r["by_rule"]["R0"] == 1 and r["ask"] == 0
    s = _stated(conn)["nk_a"]
    assert s["rule_id"] == "R0" and s["recorded_by"] == "system" and s["verbatim_utterance_id"] == u
    assert _span_text(conn, s) == "please keep weekly_totals on fiscal weeks!"
    assert s["evidence_level"] == "verbal"
    assert [(x[0], x[1]) for x in conn.execute("SELECT rule_id, verdict FROM trigger_log WHERE plan_id=?", (PLAN,))] == [("R0", "auto")]


def test_r0_short_or_stopword_local_names_never_hit(conn, tmp_path):
    graph = _graph(tmp_path, keys=("nk_b",))          # local name `run`: a stopword and 3 chars
    _plan(conn)
    _utt(conn, "run it again and check pkg.m.run is untouched? no wait, run only")
    r = _eval(conn, graph)
    assert r["by_rule"]["R0"] == 1                     # the QUALIFIED name pkg.m.run is fine — no length rule there
    # a second plan whose only words are the bare local name `run`
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES ('P2', 'g', 'IN_PROGRESS', 'proj', 'declared', '2026-09-15 11:00:00')")
    g3 = ps.build(tmp_path / "g3.db"); ps.add_run(g3, 1, plan_id="P2"); ps.add_snapshot(g3, 1, "nk_b", "pkg.m.run"); ps.add_event(g3, 1, 1, "node_changed", "nk_b", '{"changed": ["struct_sig"]}'); g3.commit(); g3.close()
    pv.insert_utterance(conn, session_id="s8", project="proj", plan_id="P2", text="run the whole thing once more", occurred_at="2026-09-15 11:01:00")
    r2 = triggers.evaluate(conn, project="proj", plan_id="P2", psg_db_path=str(tmp_path / "g3.db"), commit=True)
    assert r2["by_rule"]["R0"] == 0 and r2["ask"] == 1


def test_r0_a_sentence_naming_four_nodes_is_generic(conn, tmp_path):
    graph = _graph(tmp_path)
    _plan(conn)
    _utt(conn, "touch weekly_totals, clean_rows, load_orders and save_all today. Also keep pkg.m.run.")
    r = _eval(conn, graph)
    assert r["by_rule"]["R0"] == 1 and _stated(conn).keys() == {"nk_b"}          # only the second, specific sentence lands
    amb = conn.execute("SELECT node_key, basis FROM trigger_log WHERE plan_id=? AND verdict='ambiguous' ORDER BY node_key", (PLAN,)).fetchall()
    assert [x[0] for x in amb] == ["nk_a", "nk_c", "nk_d", "nk_e"] and all("names 4 nodes" in x[1] for x in amb)
    assert r["ask"] == 4                                                            # generic sentences do not answer for them


def test_r0_file_basename_hits(conn, tmp_path):
    graph = _graph(tmp_path, keys=("nk_a", "nk_c"))
    _plan(conn)
    _utt(conn, "rollup.py must stay pure. clean.py can grow.")
    r = _eval(conn, graph)
    assert r["by_rule"]["R0"] == 2
    st = _stated(conn)
    assert _span_text(conn, st["nk_a"]) == "rollup.py must stay pure." and _span_text(conn, st["nk_c"]) == "clean.py can grow."


def test_r0_partial_token_overlap_and_user_query_do_not_count(conn, tmp_path):
    graph = _graph(tmp_path, keys=("nk_a",))
    _plan(conn, user_query="weekly_totals should keep fiscal weeks")     # Plans.user_query is not an utterance
    _utt(conn, "weekly totals look wrong; weekly_totals_v2 is not this one; WEEKLY_TOTALS neither")
    r = _eval(conn, graph)
    assert r["by_rule"]["R0"] == 0 and r["ask"] == 1
    ctx = triggers._ctx(conn, "proj", PLAN, graph)
    assert triggers.r0_user_words(ctx, ctx.touched["nk_a"]) is None


def test_r0_same_session_utterance_without_plan_counts(conn, tmp_path):
    graph = _graph(tmp_path, keys=("nk_c",))
    _plan(conn)
    _utt(conn, "start with the plan", plan_id=PLAN, session="s7")
    _utt(conn, "and clean_rows must drop nulls first", plan_id=None, session="s7", project=None, at="2026-09-15 09:00:00")   # outside the window, same session
    _utt(conn, "clean_rows in another session is not us", plan_id=None, session="s9", project=None, at="2026-09-15 09:00:00")
    r = _eval(conn, graph)
    assert r["by_rule"]["R0"] == 1
    assert _span_text(conn, _stated(conn)["nk_c"]) == "and clean_rows must drop nulls first"


def test_draft_ranks_r0_hits_first_with_the_sentence_span(conn, tmp_path):
    graph = _graph(tmp_path, keys=("nk_a",))
    _plan(conn)
    a = _utt(conn, "the weekly totals and the rollup feel off")                 # token overlap only
    b = _utt(conn, "keep weekly_totals on fiscal weeks. thanks")                # literal R0 hit
    out = {x["node_key"]: x for x in reasons.draft(conn, "proj", PLAN, graph)}
    cands = out["nk_a"]["candidates"]
    assert [c["utterance_id"] for c in cands] == [b, a] and cands[0]["score"] >= 10 and cands[0]["kind"] == "r0"
    t = conn.execute("SELECT text FROM utterance WHERE id=?", (b,)).fetchone()[0]
    assert t[cands[0]["span"][0]:cands[0]["span"][1]] == "keep weekly_totals on fiscal weeks."
    assert cands[1]["span"] == [0, len(conn.execute("SELECT text FROM utterance WHERE id=?", (a,)).fetchone()[0])]


def test_cli_reasons_ask_basis_groups_by_plan_and_node_type(conn, tmp_path, monkeypatch):
    graph = _graph(tmp_path, keys=("nk_a", "nk_c"))
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "proj", "repo": str(tmp_path), "db_path": graph, "commit_sha": "c"}]}))
    _plan(conn)
    _eval(conn, graph)                                                           # two asks, no utterance
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PSG_REGISTRY_PATH=str(reg), PYTHONPATH=str(REPO / "orchestrator-backend"))
    r = subprocess.run([sys.executable, "-m", "orchestrator.cli", "reasons", "ask-basis", "--since", "2026-09-15"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["asks"] == 2 and out["groups"] == [{"plan_id": PLAN, "project": "proj", "node_type": "function", "asks": 2, "basis": ["no rule recognised the change"]}]
    r = subprocess.run([sys.executable, "-m", "orchestrator.cli", "reasons", "ask-basis", "--since", "2027-01-01"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads(r.stdout)["asks"] == 0
