"""`provledger why` — one bounded, self-describing read (DP phase 2, Task 4)."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import constraints, context_pack as cp, db, provenance as pv, psg_bridge

try:
    from orchestrator import why
except ImportError:                       # RED: the module does not exist yet
    why = None

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
BANNED = ("blame", "scapegoat", "pass the buck", "for the boss")


def _snap(c, run, key, qn, lo, hi, ntype="function"):
    c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, line_start, line_end, struct_sig, dataflow_trivial, attrs_json) "
              "VALUES (?, ?, ?, ?, 'pkg/m.py', ?, ?, 's', 1, '{}')", (run, key, ntype, qn, lo, hi))


@pytest.fixture
def graph(tmp_path):
    """nk_a pkg.m.load (run 1) → renamed pkg.m.load_orders (run 2), lines 10–40; nk_i inner 20–25; nk_x pkg.m.clean 50–60 eats nk_a's output."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0"); ps.add_run(c, 2, plan_id="P1"); ps.add_run(c, 3, plan_id="P2")
    _snap(c, 1, "nk_a", "pkg.m.load", 10, 40); ps.add_event(c, 1, 1, "node_added", "nk_a")
    _snap(c, 2, "nk_a", "pkg.m.load_orders", 10, 40); ps.add_event(c, 2, 1, "node_renamed", "nk_a", '{"from": "pkg.m.load", "to": "pkg.m.load_orders"}')
    _snap(c, 3, "nk_a", "pkg.m.load_orders", 10, 40); ps.add_event(c, 3, 1, "node_changed", "nk_a", '{"changed": ["struct_sig"]}')
    _snap(c, 3, "nk_i", "pkg.m.load_orders.inner", 20, 25); ps.add_event(c, 3, 2, "node_added", "nk_i")
    _snap(c, 3, "nk_x", "pkg.m.clean", 50, 60); ps.add_event(c, 3, 3, "node_added", "nk_x")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
        INSERT INTO node_type (id, name) VALUES (1, 'function');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (7, 1, 'load_orders', 'pkg.m.load_orders', 'pkg/m.py', 3, 'nk_a');
        INSERT INTO consistency_card VALUES (7, '{"callers": ["pkg.m.main"], "callees": [], "output_consumers": ["pkg.m.clean"], "dtype_map": {}, "lineage_downstream": [], "reads": []}');
    """)
    c.commit(); c.close()
    return str(path)


def _seed(conn):
    """2 constraints on nk_a (one superseded), 1 rejected path, 2 reasons (one under the OLD name), 1 unstated."""
    c1 = constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="keep paid orders only", rationale="finance")
    c2 = constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="old rule", rationale="x")
    pv.supersede(conn, c2, c1)
    rj = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical", role="rejected_path", interpretation="tried dropping nulls first", rule_id="R6", recorded_by="system")
    r1 = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical", interpretation="weekly grain because finance", recorded_by="agent")
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P1", text="rename load to load_orders", occurred_at="2026-09-15 09:00:00")
    r2 = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical", verbatim=(u, 0, 26), recorded_by="agent")
    un = pv.insert_reason(conn, project="proj", plan_id="P2", node_key="nk_a", kind="technical", recorded_by="system")
    return {"c1": c1, "c2": c2, "rj": rj, "r1": r1, "r2": r2, "un": un}


def test_three_target_forms_resolve_to_the_same_node_and_file_line_is_innermost(conn, graph):
    _seed(conn)
    for t in ("pkg.m.load_orders", "nk_a", "pkg/m.py:12", str(Path("/repo") / "pkg/m.py") + ":12"):
        out = why.why(conn, project="proj", target=t, psg_db_path=graph, record=False)
        assert out["doc"]["resolved"]["node_key"] == "nk_a", t
        assert out["text"].startswith("pkg.m.load_orders · "), t
    inner = why.why(conn, project="proj", target="pkg/m.py:22", psg_db_path=graph, record=False)
    assert inner["doc"]["resolved"]["node_key"] == "nk_i"                      # the innermost node wins
    assert why.resolve_target(graph, "pkg/m.py:999")["node_key"] is None


def test_summary_line_is_fixed_and_counts_everything_even_what_is_not_shown(conn, graph):
    ids = _seed(conn)
    out = why.why(conn, project="proj", target="pkg.m.load_orders", psg_db_path=graph, record=False)
    assert out["text"].splitlines()[0] == "pkg.m.load_orders · downstream 1 · history 3 · constraints 2 (1 active) · rejected 1 · pending 1"
    s = out["doc"]["summary"]
    assert s["identity_chain"] == ["pkg.m.load_orders", "pkg.m.load"] and s["constraints_active"] == 1 and s["pending"] == 1
    # every record line: #id · tier · source level · when · shown n · adopted-by
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment) VALUES (?, 'proj', 'P1', 'plan')", (ids["c1"],))
    conn.execute("INSERT INTO influence (reason_id, project, plan_id, via, by) VALUES (?, 'proj', 'P9', 'headline_response', 'agent')", (ids["c1"],))
    conn.commit()
    text = why.why(conn, project="proj", target="nk_a", psg_db_path=graph, record=False)["text"]
    assert f"#{ids['c1']} · asserted · task context · " in text and "shown 1 · adopted by P9" in text
    assert f"#{ids['r2']} · stated · verbal · " in text and text.count("shown 0 · not adopted") >= 3
    assert "rename load to load_orders" in text                                 # the stated span, verbatim
    # FL-153: the folded line states the TOTAL callers, not what the cap kept, and
    # the command it names is the one that actually prints the names.
    assert "── blast radius: callers 1 · output consumers 1 · lineage downstream 0 — no names printed" in text
    assert "pkg.m.clean · constraints 0" in why.why(conn, project="proj", target="nk_a", psg_db_path=graph, impact=True, record=False)["text"]


def test_budget_trims_in_order_with_counts_and_all_lifts_the_caps(conn, graph):
    _seed(conn)
    for i in range(6):
        pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical", interpretation=f"reason number {i} " + "x" * 80, recorded_by="agent")
        pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical", role="rejected_path", interpretation=f"rejected {i} " + "y" * 80, rule_id="R6", recorded_by="system")
    tight = why.why(conn, project="proj", target="nk_a", psg_db_path=graph, budget=250, record=False)
    assert tight["doc"]["truncated"].get("reasons", 0) > 0                       # reasons go first
    assert any("not expanded" in h and "provledger why" in h for h in tight["doc"]["hints"])
    assert any("not expanded" in ln for ln in tight["text"].splitlines())
    everything = why.why(conn, project="proj", target="nk_a", psg_db_path=graph, all_records=True, record=False)
    assert len([r for r in everything["doc"]["records"] if r["layer"] == "reasons"]) == 8         # 2 seeded + 6, no cap
    assert len([r for r in everything["doc"]["records"] if r["layer"] == "rejected"]) == 7


def test_pending_and_never_read_lists(conn, graph):
    ids = _seed(conn)
    p = why.why(conn, project="proj", target="nk_a", psg_db_path=graph, pending_only=True, record=False)
    assert f"#{ids['un']} · unstated · P2" in p["text"] and p["doc"]["shown"] == 0
    allp = why.why(conn, project="proj", pending_only=True, record=False)
    assert allp["doc"]["mode"] == "pending" and [r["id"] for r in allp["doc"]["records"]] == [ids["un"]]
    nr = why.why(conn, project="proj", never_read_only=True, psg_db_path=graph, record=True)
    assert nr["doc"]["mode"] == "never-read" and [r["id"] for r in nr["doc"]["records"]] == [ids["c1"]]   # superseded c2 is not active
    assert "active constraints never shown to anyone · 1" in nr["text"]
    # showing it in --never-read IS showing it: now it has a read_hit(why)
    assert why.why(conn, project="proj", never_read_only=True, psg_db_path=graph, record=False)["doc"]["records"] == []
    assert conn.execute("SELECT moment FROM read_hit WHERE reason_id=?", (ids["c1"],)).fetchone()[0] == "why"


def test_search_hits_with_fts_and_degrades_to_like_when_the_module_is_missing(conn, graph, monkeypatch):
    ids = _seed(conn)
    hit = why.why(conn, project="proj", search_query="paid", psg_db_path=graph, record=False)
    assert [r["id"] for r in hit["doc"]["records"]] == [ids["c1"]]
    if not hit["doc"]["degraded"]:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='change_reason_fts'").fetchone()
        assert not hit["text"].startswith("(no full-text index")
        later = pv.insert_reason(conn, project="proj", plan_id="P2", node_key="nk_a", kind="technical", interpretation="paid twice", recorded_by="agent")
        assert later in [r["id"] for r in why.why(conn, project="proj", search_query="paid", record=False)["doc"]["records"]]   # trigger keeps it in sync
    else:
        print("SKIP-NOTE: this sqlite has no FTS5 — only the LIKE path was exercised")

    def boom(*a, **k):
        raise sqlite3.OperationalError("no such module: fts5")
    monkeypatch.setattr(why, "_fts_query", boom)
    monkeypatch.setattr(why, "ensure_fts", lambda c: True)
    deg = why.why(conn, project="proj", search_query="paid", record=False)
    assert deg["doc"]["degraded"] is True and deg["text"].splitlines()[0] == "(no full-text index here — this search fell back to LIKE)"
    assert ids["c1"] in [r["id"] for r in deg["doc"]["records"]]


def test_every_record_printed_is_a_read_hit_of_moment_why(conn, graph):
    ids = _seed(conn)
    out = why.why(conn, project="proj", target="nk_a", psg_db_path=graph, session_id="sess-1")
    shown = out["doc"]["shown"]
    rows = conn.execute("SELECT reason_id, moment, session_id, plan_id FROM read_hit ORDER BY reason_id").fetchall()
    assert shown == 4 and len(rows) == shown and {r[1] for r in rows} == {"why"} and {r[2] for r in rows} == {"sess-1"}
    assert {r[0] for r in rows} == {ids["c1"], ids["rj"], ids["r1"], ids["r2"]}          # the superseded constraint and the unstated row are not "shown"
    why.why(conn, project="proj", target="nk_a", psg_db_path=graph)                    # no plan id: a second look is a second hit
    assert conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0] == 2 * shown
    assert conn.execute("SELECT COUNT(*) FROM influence").fetchone()[0] == 0           # I11: reading is never adopting
    st = why.stats_for(conn, [ids["c1"]])[ids["c1"]]
    assert st["shown"] == 2 and st["by_moment"] == {"why": 2}


def _cli(conn, *argv, env_extra=None, cwd=None):
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"))
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True, env=env, cwd=cwd or str(REPO))


def test_cli_why_prints_the_read_and_help_has_no_banned_words(conn, graph, tmp_path):
    _seed(conn)
    reg = tmp_path / "projects.json"
    reg.write_text('{"projects": [{"name": "proj", "repo": "/x", "db_path": "%s", "commit_sha": "c"}]}' % graph)
    env = {"PSG_REGISTRY_PATH": str(reg)}
    r = _cli(conn, "why", "pkg.m.load_orders", "--project", "proj", env_extra=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == "pkg.m.load_orders · downstream 1 · history 3 · constraints 2 (1 active) · rejected 1 · pending 1"
    assert conn.execute("SELECT COUNT(*) FROM read_hit WHERE moment='why'").fetchone()[0] == 4
    j = _cli(conn, "why", "nk_a", "--project", "proj", "--json", env_extra=env)
    assert j.returncode == 0 and '"summary"' in j.stdout
    nothing = _cli(conn, "why", "pkg.m.nope", "--project", "proj", env_extra=env)
    assert nothing.returncode == 0 and "pkg.m.nope" in nothing.stdout
    for argv in (["why", "--help"], ["export", "--help"], ["init", "--help"], ["headline", "--help"]):
        h = _cli(conn, *argv)
        assert h.returncode == 0 and not any(w in h.stdout for w in BANNED), argv
    assert "evidence level" not in _cli(conn, "why", "--help").stdout
    top = " ".join(_cli(conn, "--help").stdout.split())        # argparse wraps; the wording is what matters
    assert "source level" in top and "evidence level" not in top


# ── FL-153: both expansion flags recommended themselves ─────────────────────
# Measured on the live ledger before the fix:
#   why …review_and_complete --all     → " 3 more reasons not expanded — `… --all`"
#   why …review_and_complete --impact  → "callers 12 … `--impact` expands", zero names
# and the two lines of ONE read disagreed: `callers 12` on the blast-radius
# line, `callers 38` in the footer. 12 was what the cap kept, 38 what it
# dropped, and 50 was the truth neither line printed.

def _many_callers(graph, n=50):
    import json as _json
    g = sqlite3.connect(graph)
    g.execute("UPDATE consistency_card SET card_json = ? WHERE symbol_id = 7",
              (_json.dumps({"callers": [f"pkg.t.test_{i}" for i in range(n)], "callees": [], "output_consumers": ["pkg.m.clean"],
                            "dtype_map": {}, "lineage_downstream": ["pkg.m.report"], "reads": []}),))
    g.commit(); g.close()


def test_all_never_tells_you_to_pass_all_again(conn, graph):
    _seed(conn)
    for i in range(6):
        pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical",
                         interpretation=f"reason number {i} " + "x" * 80, recorded_by="agent")
        pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical", role="rejected_path",
                         interpretation=f"rejected {i} " + "y" * 80, rule_id="R6", recorded_by="system")
    out = why.why(conn, project="proj", target="nk_a", psg_db_path=graph, all_records=True, record=False)
    assert not [h for h in out["doc"]["hints"] if "--all" in h], out["doc"]["hints"]
    assert not [ln for ln in out["text"].splitlines() if "not expanded" in ln]
    assert out["doc"]["truncated"].get("reasons", 0) == 0 and out["doc"]["truncated"].get("rejected_paths", 0) == 0


def test_impact_prints_the_caller_names_and_never_recommends_impact_again(conn, graph):
    _seed(conn)
    _many_callers(graph)
    out = why.why(conn, project="proj", target="nk_a", psg_db_path=graph, impact=True, record=False)
    text = out["text"]
    assert "── blast radius · callers 50 · output consumers 1 · lineage downstream 1" in text
    assert text.count("caller · pkg.t.test_") == 50                  # names, not a count: a name can be fed back in
    assert "lineage downstream · pkg.m.report" in text
    assert "output consumer · pkg.m.clean · constraints 0" in text
    assert not [h for h in out["doc"]["hints"] if "--impact" in h], out["doc"]["hints"]
    assert out["doc"]["truncated"].get("callers", 0) == 0
    assert out["doc"]["blast_radius"]["callers"] == [f"pkg.t.test_{i}" for i in range(50)]


def test_the_folded_blast_radius_line_prints_the_total_and_names_nothing_twice(conn, graph):
    """One read may not carry two different numbers for the same thing. Folded,
    the line states the total (50) and the one command that names them; the
    footer no longer repeats a different figure for the same fold."""
    _seed(conn)
    _many_callers(graph)
    out = why.why(conn, project="proj", target="nk_a", psg_db_path=graph, record=False)
    text = out["text"]
    assert "── blast radius: callers 50 · output consumers 1 · lineage downstream 1 — no names printed" in text
    assert "provledger why pkg.m.load_orders --impact" in text
    assert "callers 12" not in text and "callers 38" not in text
    assert len([ln for ln in text.splitlines() if "callers" in ln]) == 1


def test_a_record_cut_at_240_chars_says_so_and_names_the_read_that_has_the_rest(conn, graph):
    """FL-154: the cut was silent, so a severed clause read as a whole record."""
    _seed(conn)
    long_text = "the whole reason is " + "z" * 900
    rid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical",
                           interpretation=long_text, recorded_by="agent")
    conn.commit()
    out = why.why(conn, project="proj", target="nk_a", psg_db_path=graph, record=False)
    assert f"+{len(long_text) - 240} chars cut" in out["text"]
    assert f"provledger record #{rid}" in out["text"]
    rec = next(r for r in out["doc"]["records"] if r["id"] == rid)
    assert rec["text_cut"] == len(long_text) - 240 and rec["text_chars"] == len(long_text)
    short = next(r for r in out["doc"]["records"] if r["id"] != rid)
    assert short["text_cut"] == 0
    assert out["text"].count("chars cut") == 1                       # an uncut record carries no marker


def test_the_remainder_the_footer_states_is_what_all_actually_reveals(conn, graph):
    """Live, after the first two fixes: `--impact` still footed "3 more reasons
    not expanded" while `--all` showed 5 of 5. The two lines disagreed because
    `build` counted the `unstated` slot among `reasons` and the renderer counted
    it under `pending` — one output, two definitions of the same word. 2 is
    right: five reasons carry text, three are printed, and the unstated slot is
    already reported on the summary line."""
    _seed(conn)                                       # 2 reasons (one stated) + 1 unstated slot on nk_a
    for i in range(3):
        pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical",
                         interpretation=f"reason number {i}", recorded_by="agent")
    conn.commit()
    folded = why.why(conn, project="proj", target="nk_a", psg_db_path=graph, record=False)
    assert "pending 1" in folded["text"].splitlines()[0]
    assert folded["doc"]["truncated"]["reasons"] == 2          # 5 with text, 3 printed — not 3
    assert any("2 more reasons not expanded" in h for h in folded["doc"]["hints"])
    every = why.why(conn, project="proj", target="nk_a", psg_db_path=graph, all_records=True, record=False)
    printed = len([r for r in every["doc"]["records"] if r["layer"] == "reasons"])
    assert printed == 5 and printed == 3 + folded["doc"]["truncated"]["reasons"]
