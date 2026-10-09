"""The close-time rules write only what is true (FL-238, FL-239).

R0 turns the user's own words into a `stated` reason. Claude Code also sends
text through UserPromptSubmit that the user never typed — a subagent's report
(`<agent-message from=…>`), a task notification, a system reminder — and an
older hook recorded some of it. Those rows stay (append-only), so the rule
that reads them has to leave them out, with the same predicate the hook uses.

R6 turns a failed step into a rejected path. It used to hang the row on the
first touched node whose local name was any word of the failure text (`run`,
`psg`, `ledger`), so `why <node>` told a story about the wrong function. It now
anchors only on a changed node the text names explicitly, else on the plan."""
import sys
from pathlib import Path

import pytest

from orchestrator import api, hooks, provenance as pv, reasons, triggers

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

PLAN = "P1"


def _graph(tmp_path, changed: dict, touched: dict = {}):
    """{node_key: qualified_name}: `changed` get a node_changed event in the
    plan's run, `touched` only node_matched."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_run(c, 2, plan_id=PLAN, step_id=f"{PLAN}-REVIEW.1")
    seq = 0
    for k, qn in {**changed, **touched}.items():
        for run, sig in ((1, "s1"), (2, "s2" if k in changed else "s1")):
            c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, "
                      "dataflow_sig, dataflow_trivial, attrs_json) VALUES (?, ?, 'function', ?, 'pkg/m.py', ?, NULL, 1, '{}')",
                      (run, k, qn, sig))
        seq += 1
        ps.add_event(c, 1, seq, "node_added", k)
        seq += 1
        ps.add_event(c, 2, seq, "node_changed" if k in changed else "node_matched", k,
                     '{"changed": ["struct_sig"]}' if k in changed else '{"via": "qualname"}')
    c.commit()
    c.close()
    return str(path)


def _plan(conn, steps=("A",)):
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES "
                 "(?, 'goal', 'IN_PROGRESS', 'proj', 'declared', '2026-09-15 10:00:00')", (PLAN,))
    for i, s in enumerate(steps):
        conn.execute("INSERT INTO Steps (step_id, plan_id, description, status, execution_order, depth_level, log_context, step_type) "
                     "VALUES (?, ?, ?, 'PENDING', ?, 0, '', 'COMMAND')", (f"{PLAN}-{s}", PLAN, f"step {s}", i))
    conn.commit()


def _utt(conn, text, at="2026-09-15 10:05:00"):
    return pv.insert_utterance(conn, session_id="s7", project="proj", plan_id=PLAN, text=text, occurred_at=at)


HANDBACK = ('<agent-message from="a1b2c3">\n[Subagent hand-back] The text below is the final report of a subagent.\n'
            "Keep weekly_totals on fiscal weeks.\n</agent-message>")
INJECTED = [HANDBACK,
            "<task-notification>\nweekly_totals finished\n</task-notification>",
            "<system-reminder>weekly_totals is on fiscal weeks</system-reminder>",
            "<command-name>/ledger</command-name> weekly_totals",
            "<local-command-caveat>weekly_totals</local-command-caveat>"]


# ── FL-238: R0 reads only the user's words ───────────────────────────────────

@pytest.mark.parametrize("text", INJECTED)
def test_an_injected_utterance_is_not_a_candidate(conn, tmp_path, text):
    graph = _graph(tmp_path, {"nk_a": "pkg.rollup.weekly_totals"})
    _plan(conn)
    _utt(conn, text)
    user = _utt(conn, "a person writing about <agent-message tags is still a person", at="2026-09-15 10:06:00")
    ctx = triggers._ctx(conn, "proj", PLAN, graph)
    assert [u["id"] for u in triggers.candidate_utterances(ctx)] == [user]


def test_a_handback_naming_a_changed_node_writes_no_stated_reason(conn, tmp_path):
    graph = _graph(tmp_path, {"nk_a": "pkg.rollup.weekly_totals", "nk_b": "pkg.rollup.load_orders"})
    _plan(conn)
    _utt(conn, HANDBACK)
    u = _utt(conn, "please keep load_orders on fiscal weeks", at="2026-09-15 10:06:00")
    triggers.evaluate(conn, project="proj", plan_id=PLAN, psg_db_path=graph, commit=True)
    stated = {r["node_key"]: r for r in pv.reasons_for_plan(conn, PLAN) if r["tier"] == "stated"}
    assert set(stated) == {"nk_b"} and stated["nk_b"]["verbatim_utterance_id"] == u


def test_the_reason_draft_proposes_nothing_from_a_handback(conn, tmp_path):
    graph = _graph(tmp_path, {"nk_a": "pkg.rollup.weekly_totals"})
    _plan(conn)
    _utt(conn, HANDBACK)
    out = {x["node_key"]: x for x in reasons.draft(conn, "proj", PLAN, graph)}
    assert out["nk_a"]["candidates"] == []


def test_the_hook_and_the_rules_share_one_predicate():
    assert hooks.is_injected_prompt is pv.is_injected_prompt
    assert hooks.INJECTED_PROMPT_PREFIXES is pv.INJECTED_PROMPT_PREFIXES


def test_no_stated_reason_can_quote_injected_text(conn):
    """reason-fill takes an utterance id and a span from the agent: the store,
    not each caller, refuses a span of text the user never typed."""
    _plan(conn)
    u = _utt(conn, HANDBACK)
    with pytest.raises(ValueError, match="not the user"):
        pv.insert_reason(conn, project="proj", plan_id=PLAN, node_key="nk_a", kind="technical",
                         verbatim=(u, 0, 20), recorded_by="agent")

# ── FL-239: R6 anchors only on an explicit name ─────────────────────────────

CHANGED = {"nk_run": "skills.review_run.Driver.run", "nk_snap": "scripts.home_guard.snapshot",
           "nk_schema": "scripts.home_guard._schema", "nk_led": "tests.test_chain_race.ledger",
           "nk_lo": "pkg.rollup.load_orders", "nk_wt": "pkg.rollup.weeklyTotals"}
TOUCHED = {"nk_psg": "tests.test_evidence_slots.psg_fixture"}


@pytest.mark.parametrize("text, anchor", [
    ("Driver.run exited 5 after the refresh", "nk_run"),
    ("home_guard crashed in snapshot() on a NULL row", "nk_snap"),
    ("`_schema` read a NULL migration_file", "nk_schema"),
    ("run the affected suites: bash scripts/run_tests.sh scripts backend; the ledger stayed locked", None),
    ("CHANGELOG review: the psg suite was red", None),
    ("rewrite load_orders filter: it kept the test rows", "nk_lo"),      # snake_case is code, not a word
    ("weeklyTotals summed the wrong column", "nk_wt"),                  # so is camelCase
    ("psg_fixture() was slow", None),                                   # only touched, never anchored
    ("psg_fixture was slow", None),
    ("both snapshot() and `_schema` changed shape", None),               # two explicit names: the plan
])
def test_r6_anchors_only_on_a_changed_node_named_explicitly(conn, tmp_path, text, anchor):
    graph = _graph(tmp_path, CHANGED, TOUCHED)
    _plan(conn)
    api.start_step(conn, f"{PLAN}-A")
    api.fail_step(conn, f"{PLAN}-A", reason=text)
    assert triggers.rejected_paths(conn, project="proj", plan_id=PLAN, psg_db_path=graph, commit=True) == 1
    (row,) = pv.reasons_for_plan(conn, PLAN, role="rejected_path")
    assert row["node_key"] == anchor and row["rule_id"] == "R6"
