"""A6: `provledger review evidence-slots --plan <id>` — the to-check list.

provLedger searches nothing. It says which changed data points are worth looking
for evidence for, inside which time window, with which identifiers to search on,
and hands that list to the host agent — which owns the mailbox, the credentials
and the tools. Whatever the host finds comes back through `reference add`.

The two gates of A6 live here: the window is the plan's own span (not the
project's history), and the list is ordered by the significance the existing
`significance.py` computes and cut to `PROVLEDGER_EVIDENCE_SLOTS`. Nothing in
this command runs a model: the hints are identifiers pulled out of the graph and
out of what was recorded, by rule.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import db, evidence, provenance as pv

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CREATED = "2026-09-25 00:00:00"
COMPLETED = "2026-09-27 14:02:11"
OLD = "2026-01-02 08:00:00"

KEYS = (("nk_a", "pkg.rollup.weekly_report"), ("nk_b", "pkg.rollup.clean_regions"),
        ("nk_c", "pkg.rollup.weekly_report:df.amount"))


def _psg(tmp_path, plan_id="P1", keys=KEYS):
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    for i, (k, qn) in enumerate(keys, 1):
        ps.add_snapshot(c, 1, k, qn)
        ps.add_event(c, 1, i, "node_added", k)
    ps.add_run(c, 2, plan_id=plan_id, step_id=f"{plan_id}-REVIEW.1")
    for i, (k, qn) in enumerate(keys, 1):
        ps.add_snapshot(c, 2, k, qn, ntype="column" if ":" in qn else "function")
        ps.add_event(c, 2, i, "node_changed", k, '{"changed": ["struct_sig"]}')
    c.commit()
    c.close()
    return str(path)


@pytest.fixture
def psg(tmp_path):
    return _psg(tmp_path)


@pytest.fixture
def registry(tmp_path, psg):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": "proj", "repo": "/x", "db_path": psg, "commit_sha": "c"}]}))
    return str(p)


@pytest.fixture
def plan(conn):
    """One older plan and one current plan, so a window that reached back over the
    project's history would be visible instead of plausible."""
    db.insert_plan(conn, "P0", "the plan before this one", created_at=OLD, project="proj", project_source="declared")
    conn.execute("UPDATE Plans SET completed_at = ?, status = 'COMPLETED' WHERE plan_id = 'P0'", (OLD,))
    db.insert_plan(conn, "P1", "drop EMEA from the Q3 rollup", created_at=CREATED,
                   project="proj", project_source="declared")
    conn.execute("UPDATE Plans SET completed_at = ? WHERE plan_id = 'P1'", (COMPLETED,))
    conn.commit()
    return "P1"


def _slots(conn, psg, plan_id="P1", **kw):
    return evidence.slots_for_plan(conn, project="proj", plan_id=plan_id, psg_db_path=psg, **kw)


def _cli(conn, *argv, env_extra=None):
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"))
    env.pop("PROVLEDGER_EVIDENCE_SLOTS", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv],
                          capture_output=True, text=True, env=env, cwd=str(REPO))


# ── (a) the shape of one slot ────────────────────────────────────────────────

def test_every_slot_carries_node_what_changed_window_hints_and_significance(conn, psg, plan):
    slots = _slots(conn, psg)
    assert slots, "the plan changed three nodes; the list may not be empty"
    for s in slots:
        assert set(("node", "what_changed", "window", "hints", "significance")) <= set(s), s
        assert isinstance(s["node"], str) and s["node"]
        assert isinstance(s["what_changed"], str) and s["what_changed"]
        assert isinstance(s["hints"], list) and s["hints"]
        assert isinstance(s["significance"], (int, float))
    assert {s["node"] for s in slots} == {qn for _, qn in KEYS}


def test_the_slot_names_the_reason_to_hang_the_pointer_on(conn, psg, plan):
    """`reference add` wants `--reason <id>`. A slot with a reason already filled
    in says which one; a slot with none says so, and the host does not invent one."""
    rid = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical",
                           interpretation="EMEA excluded from the Q3 rollup")
    by_node = {s["node_key"]: s for s in _slots(conn, psg)}
    assert by_node["nk_a"]["reason_id"] == rid
    assert by_node["nk_a"]["reason_tier"] == "asserted"
    assert by_node["nk_b"]["reason_id"] is None
    assert by_node["nk_b"]["reason_tier"] is None


def test_what_changed_repeats_the_recorded_reason_rather_than_inventing_prose(conn, psg, plan):
    pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical",
                     interpretation="EMEA excluded from the Q3 rollup")
    by_node = {s["node_key"]: s for s in _slots(conn, psg)}
    assert by_node["nk_a"]["what_changed"] == "EMEA excluded from the Q3 rollup"
    # nothing was recorded for nk_b, so what_changed states the graph event and nothing more
    assert "nk_b" not in by_node["nk_b"]["what_changed"]
    assert "pkg.rollup.clean_regions" in by_node["nk_b"]["what_changed"]
    assert "node_changed" in by_node["nk_b"]["what_changed"]


# ── (b) the window is this plan's, not the project's ────────────────────────

def test_window_is_the_plans_own_span(conn, psg, plan):
    for s in _slots(conn, psg):
        assert s["window"] == [CREATED, COMPLETED], s["window"]


def test_window_does_not_reach_back_over_the_project_history(conn, psg, plan):
    """P0 closed in January. A window starting there would send the host agent
    through eight months of mailbox for a two-day change — the cost gate of A6."""
    starts = {tuple(s["window"])[0] for s in _slots(conn, psg)}
    assert starts == {CREATED}
    assert OLD not in starts


def test_an_open_plan_ends_its_window_now_rather_than_at_the_end_of_time(conn, psg, plan):
    conn.execute("UPDATE Plans SET completed_at = NULL WHERE plan_id = 'P1'")
    conn.commit()
    now = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now')").fetchone()[0]
    for s in _slots(conn, psg):
        assert s["window"][0] == CREATED
        assert CREATED < s["window"][1] <= now, s["window"]


# ── (c) descending significance, and the cap ────────────────────────────────

def test_slots_come_back_in_descending_significance(conn, psg, plan):
    """An active constraint anchored on nk_b is one more of the six signals
    significance.py already counts, so nk_b outranks the others."""
    pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_b", kind="organizational",
                     role="constraint", statement="Q3 excludes EMEA")
    slots = _slots(conn, psg)
    assert slots[0]["node_key"] == "nk_b", [(s["node_key"], s["significance"]) for s in slots]
    values = [s["significance"] for s in slots]
    assert values == sorted(values, reverse=True)
    assert values[0] > values[-1], "the constraint must make a visible difference"


def test_significance_is_the_existing_hint_and_its_basis(conn, psg, plan):
    from orchestrator import significance
    slots = _slots(conn, psg)
    for s in slots:
        level, basis = significance.hint(conn, {"project": "proj", "plan_id": "P1", "node_key": s["node_key"],
                                                "run_id": s["run_id"], "tier": s["reason_tier"]}, psg)
        assert s["significance_level"] == level
        assert s["significance_basis"] == basis


def test_the_cap_is_five_by_default(conn, tmp_path, plan):
    wide = tmp_path / "wide"
    wide.mkdir()
    keys = tuple((f"nk_{i}", f"pkg.rollup.node_{i}") for i in range(7))
    assert len(_slots(conn, _psg(wide, keys=keys))) == 5
    assert evidence.DEFAULT_SLOTS == 5


def test_the_cap_is_PROVLEDGER_EVIDENCE_SLOTS(conn, psg, plan, monkeypatch):
    monkeypatch.setenv("PROVLEDGER_EVIDENCE_SLOTS", "2")
    assert len(_slots(conn, psg)) == 2
    monkeypatch.setenv("PROVLEDGER_EVIDENCE_SLOTS", "0")
    assert _slots(conn, psg) == []


def test_a_cap_that_is_not_a_number_falls_back_to_the_default(conn, psg, plan, monkeypatch):
    monkeypatch.setenv("PROVLEDGER_EVIDENCE_SLOTS", "lots")
    assert len(_slots(conn, psg)) == 3


# ── (d) the hints are a rule, not a model ───────────────────────────────────

def test_hints_are_the_identifiers_of_the_node_and_of_what_was_recorded(conn, psg, plan):
    pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical",
                     interpretation="EMEA excluded from the Q3 rollup")
    hints = {s["node_key"]: s["hints"] for s in _slots(conn, psg)}
    assert hints["nk_a"] == ["weekly_report", "rollup", "EMEA", "Q3"], hints["nk_a"]
    # a plain lower-case word is not an identifier and is not sent off to be searched
    assert "excluded" not in hints["nk_a"] and "the" not in hints["nk_a"]
    assert hints["nk_c"][0] == "amount"


def test_hints_are_deterministic(conn, psg, plan):
    assert [s["hints"] for s in _slots(conn, psg)] == [s["hints"] for s in _slots(conn, psg)]


def test_hint_extraction_is_a_pure_function_of_the_two_strings():
    assert evidence.hints_for("pkg.rollup.weekly_report", "EMEA excluded from the Q3 rollup") == \
        ["weekly_report", "rollup", "EMEA", "Q3"]
    assert evidence.hints_for("pkg.rollup.weekly_report", "EMEA excluded from the Q3 rollup") == \
        evidence.hints_for("pkg.rollup.weekly_report", "EMEA excluded from the Q3 rollup")


NO_MODEL = ("runner", "llm", "prompt", "judge", "claude", "openai")


def test_nothing_in_the_slot_builder_calls_a_model(conn, psg, plan):
    src = (REPO / "orchestrator-backend" / "orchestrator" / "evidence.py").read_text(encoding="utf-8").lower()
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for word in NO_MODEL:
        assert word not in body, f"evidence slots must not reach for {word}"


FETCHERS = ("urlopen", "urllib", "requests", "httpx", "webfetch", "socket", "curl")


def test_nothing_in_the_slot_builder_fetches_anything(conn, psg, plan):
    src = (REPO / "orchestrator-backend" / "orchestrator" / "evidence.py").read_text(encoding="utf-8").lower()
    for f in FETCHERS:
        assert f not in src, f"provLedger must not reach for {f}: the host owns the credentials"


# ── (e) a slot that no longer needs evidence, and a plan with nothing to explain ──

def test_a_reason_that_already_has_a_linked_pointer_is_not_a_slot(conn, psg, plan):
    rid = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical",
                           interpretation="EMEA excluded from the Q3 rollup")
    ref = pv.insert_reference(conn, project="proj", kind="email", label="re: Q3 scope · sarah",
                              occurred_at=CREATED, uri="https://outlook/items/AAQk")
    pv.link_reference(conn, rid, ref)
    assert pv.get_reason(conn, rid)["evidence_level"] == "linked"
    assert {s["node_key"] for s in _slots(conn, psg)} == {"nk_b", "nk_c"}


def test_a_plan_with_nothing_to_explain_is_an_empty_list_and_not_an_error(conn, psg):
    db.insert_plan(conn, "P9", "nothing touched", created_at=CREATED, project="proj", project_source="declared")
    assert _slots(conn, psg, plan_id="P9") == []


def test_no_graph_at_all_is_an_empty_list_and_not_an_error(conn, plan):
    assert evidence.slots_for_plan(conn, project="proj", plan_id="P1", psg_db_path=None) == []


# ── (f) the command ─────────────────────────────────────────────────────────

def test_the_command_prints_the_slots_as_json(conn, psg, plan, registry):
    r = _cli(conn, "review", "evidence-slots", "--plan", "P1", "--json",
             env_extra={"PSG_REGISTRY_PATH": registry})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["plan_id"] == "P1" and out["project"] == "proj"
    assert len(out["slots"]) == 3
    assert [s["window"] for s in out["slots"]] == [[CREATED, COMPLETED]] * 3
    assert all(s["hints"] for s in out["slots"])


def test_the_command_honours_the_cap(conn, psg, plan, registry):
    r = _cli(conn, "review", "evidence-slots", "--plan", "P1", "--json",
             env_extra={"PSG_REGISTRY_PATH": registry, "PROVLEDGER_EVIDENCE_SLOTS": "1"})
    assert r.returncode == 0, r.stderr
    assert len(json.loads(r.stdout)["slots"]) == 1


def test_the_command_says_nothing_to_explain_rather_than_failing(conn, psg, registry):
    db.insert_plan(conn, "P9", "nothing touched", created_at=CREATED, project="proj", project_source="declared")
    r = _cli(conn, "review", "evidence-slots", "--plan", "P9", "--json",
             env_extra={"PSG_REGISTRY_PATH": registry})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["slots"] == []


def test_a_plan_that_does_not_exist_is_refused_with_exit_2(conn, psg, registry):
    r = _cli(conn, "review", "evidence-slots", "--plan", "nope", "--json",
             env_extra={"PSG_REGISTRY_PATH": registry})
    assert r.returncode == 2, r.stdout
    assert "nope" in r.stderr, r.stderr


def test_the_command_writes_nothing(conn, psg, plan, registry):
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("change_reason", "reference", "reference_link", "utterance")]
    r = _cli(conn, "review", "evidence-slots", "--plan", "P1", "--json",
             env_extra={"PSG_REGISTRY_PATH": registry})
    assert r.returncode == 0, r.stderr
    after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("change_reason", "reference", "reference_link", "utterance")]
    assert before == after, "evidence-slots reads; the host agent writes"
