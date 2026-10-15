"""`provledger review evidence-slots --plan <id>` — the to-check list, one per task.

provLedger searches nothing. It says which of a plan's tasks still have no
checkable source for why they were done, inside which time window, with which
identifiers to search on, and where a source that turns up should hang — and
hands that list to the host agent, which owns the mailbox, the credentials and
the tools. Whatever the host finds comes back through `reference add`.

Since step 3b of the task-level redesign a slot is a task, not a changed node:
what is looked for is the source of the task's root (the meeting or the mail
where the cause was set), once. A task whose root already has a linked source
is not a slot. The window is still the plan's own span, the list is cut to
`PROVLEDGER_EVIDENCE_SLOTS`, and nothing here runs a model.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import db, evidence, provenance as pv, reasons, roots

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CREATED = "2026-09-25 00:00:00"
COMPLETED = "2026-09-27 14:02:11"
OLD = "2026-01-02 08:00:00"
SAID = "Finance wants EMEA out of the Q3 rollup until the entity closes its books on time."

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


@pytest.fixture
def said(conn, plan):
    return pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P1", text=SAID, occurred_at=CREATED)


def _task(conn, psg, plan_id="P1", **task):
    out = reasons.fill_tasks(conn, project="proj", plan_id=plan_id, run_id=2, psg_db_path=psg,
                             tasks=[task or {"interpretation": "EMEA excluded from the Q3 rollup"}])
    return out["reason_ids"][0]


def _slots(conn, psg, plan_id="P1", **kw):
    return evidence.slots_for_plan(conn, project="proj", plan_id=plan_id, psg_db_path=psg, **kw)


def _linked(conn, label="re: Q3 scope · sarah"):
    return pv.insert_reference(conn, project="proj", kind="email", label=label, occurred_at=CREATED,
                               uri="https://mail.example.invalid/items/AAQk")


def _cli(conn, *argv, env_extra=None):
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"))
    env.pop("PROVLEDGER_EVIDENCE_SLOTS", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv],
                          capture_output=True, text=True, env=env, cwd=str(REPO))


# ── (a) one slot per task ────────────────────────────────────────────────────

def test_one_slot_per_task_reason_with_the_nodes_it_covers(conn, psg, plan):
    rid = _task(conn, psg)
    (s,) = _slots(conn, psg)
    assert (s["reason_id"], s["plan_id"], s["tier"]) == (rid, "P1", "asserted")
    assert s["task"] == "EMEA excluded from the Q3 rollup"
    assert s["nodes"] == sorted(qn for _, qn in KEYS)
    assert s["window"] == [CREATED, COMPLETED] and s["hints"]
    assert "node" not in s and "node_key" not in s


def test_a_plan_that_did_two_things_gets_a_slot_per_task_each_with_its_own_nodes(conn, psg, plan):
    out = reasons.fill_tasks(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg,
                             bundle="both came out of the same review",
                             tasks=[{"interpretation": "the amount column is in cents now", "nodes": ["nk_c"]},
                                    {"interpretation": "EMEA excluded from the Q3 rollup"}])
    slots = _slots(conn, psg)
    assert [s["reason_id"] for s in slots] == out["reason_ids"]
    assert slots[0]["nodes"] == ["pkg.rollup.weekly_report:df.amount"]
    assert slots[1]["nodes"] == ["pkg.rollup.clean_regions", "pkg.rollup.weekly_report"]


def test_an_unstated_task_is_a_slot_too(conn, psg, plan):
    """Found evidence hangs on the gap: nobody said why, and here is a source
    that may be related."""
    rid = _task(conn, psg, unstated=True)
    assert [(s["reason_id"], s["tier"], s["task"]) for s in _slots(conn, psg)] == [(rid, "unstated", None)]


def test_a_plan_closed_before_task_reasons_has_no_slot(conn, psg, plan):
    """Per-node reasons from the old close are left as they are: what is
    looked for now is a task's root, and that plan recorded no task."""
    pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical",
                     interpretation="EMEA excluded from the Q3 rollup", recorded_by="agent")
    assert _slots(conn, psg) == []


def test_a_plan_with_nothing_recorded_is_an_empty_list_and_not_an_error(conn, psg):
    db.insert_plan(conn, "P9", "nothing touched", created_at=CREATED, project="proj", project_source="declared")
    assert _slots(conn, psg, plan_id="P9") == []


def test_without_a_graph_the_slot_still_comes_with_no_nodes(conn, psg, plan):
    _task(conn, psg)
    (s,) = evidence.slots_for_plan(conn, project="proj", plan_id="P1", psg_db_path=None)
    assert s["nodes"] == [] and s["task"] == "EMEA excluded from the Q3 rollup"


# ── (b) where a found source hangs ───────────────────────────────────────────

def test_a_source_hangs_on_the_roots_words_when_the_root_names_them(conn, psg, plan, said):
    roots.record(conn, plan_id="P1", root={"kind": "new", "utterance_id": said})
    _task(conn, psg)
    (s,) = _slots(conn, psg)
    assert s["attach_to"] == {"utterance_id": said}
    assert s["root"] == {"root_plan_id": "P1", "words": SAID, "utterance_id": said}
    assert f"--utterance {said}" in s["attach_with"]


def test_without_the_roots_words_it_hangs_on_the_root_plans_task_reason(conn, psg, plan):
    roots.record(conn, plan_id="P0", root={"kind": "new", "basis": "the Q3 scope"})
    first = _task(conn, psg, plan_id="P0", interpretation="the Q3 scope was set by finance")
    roots.record(conn, plan_id="P1", root={"kind": "continues", "plan_id": "P0"})
    _task(conn, psg)
    (s,) = _slots(conn, psg)
    assert s["attach_to"] == {"reason_id": first}
    assert s["root"]["root_plan_id"] == "P0" and s["root"]["utterance_id"] is None


def test_with_no_root_it_hangs_on_the_tasks_own_reason(conn, psg, plan):
    rid = _task(conn, psg)
    (s,) = _slots(conn, psg)
    assert s["attach_to"] == {"reason_id": rid} and s["root"] is None
    assert f"--reason {rid}" in s["attach_with"]


# ── (c) a task whose root already has a linked source is not a slot ─────────

def _anchor(conn, psg, said, where):
    """Write a task whose root has a source hung at `where`; return its reason id."""
    if where == "root plan's task":
        roots.record(conn, plan_id="P0", root={"kind": "new"})
        first = _task(conn, psg, plan_id="P0", interpretation="the Q3 scope was set by finance")
        roots.record(conn, plan_id="P1", root={"kind": "continues", "plan_id": "P0"})
        pv.link_reference(conn, first, _linked(conn))
        return _task(conn, psg)
    if where == "root's words":
        roots.record(conn, plan_id="P1", root={"kind": "new", "utterance_id": said})
        rid = _task(conn, psg)
        pv.link_reference(conn, None, _linked(conn), utterance_id=said)
        return rid
    if where == "quoted words":
        rid = _task(conn, psg, utterance_id=said, span=[0, len(SAID)])
        pv.link_reference(conn, None, _linked(conn), utterance_id=said)      # after the reason: no carry-over
        return rid
    rid = _task(conn, psg)
    pv.link_reference(conn, rid, _linked(conn))
    return rid


@pytest.mark.parametrize("where", ["reason", "quoted words", "root's words", "root plan's task"])
def test_a_linked_source_reaching_the_task_removes_its_slot(conn, psg, plan, said, where):
    rid = _anchor(conn, psg, said, where)
    assert [s["via"] for s in evidence.sources_for_reason(conn, rid)] == [where]
    assert _slots(conn, psg) == []


def test_a_source_with_no_link_does_not_remove_the_slot_and_is_listed(conn, psg, plan):
    rid = _task(conn, psg)
    ref = pv.insert_reference(conn, project="proj", kind="meeting", label="Q3 planning, 2026-09-24",
                              occurred_at=CREATED)
    pv.link_reference(conn, rid, ref)
    (s,) = _slots(conn, psg)
    assert [(x["reference_id"], x["verifiability"]) for x in s["sources"]] == [(ref, "unreachable")]


# ── (d) the sources of a reason: the one read ───────────────────────────────

def test_sources_for_reason_carries_who_said_it_and_their_words(conn, psg, plan, said):
    roots.record(conn, plan_id="P1", root={"kind": "new", "utterance_id": said})
    rid = _task(conn, psg)
    ref = pv.insert_reference(conn, project="proj", kind="meeting", label="Q3 planning, 2026-09-24",
                              occurred_at="2026-09-24 10:00", uri="notes/2026-09-24-q3.md", said_by="the finance lead",
                              excerpt="EMEA stays out of Q3: the entity closes a month late.")
    pv.link_reference(conn, None, ref, utterance_id=said)
    (src,) = evidence.sources_for_reason(conn, rid)
    assert src["reference_id"] == ref and src["via"] == "root's words"
    assert (src["said_by"], src["excerpt"]) == ("the finance lead", "EMEA stays out of Q3: the entity closes a month late.")
    assert (src["kind"], src["label"], src["uri"], src["occurred_at"]) == \
        ("meeting", "Q3 planning, 2026-09-24", "notes/2026-09-24-q3.md", "2026-09-24 10:00")


def test_a_source_reached_two_ways_is_listed_once_by_the_nearest(conn, psg, plan, said):
    roots.record(conn, plan_id="P1", root={"kind": "new", "utterance_id": said})
    rid = _task(conn, psg, utterance_id=said, span=[0, 20])
    ref = _linked(conn)
    pv.link_reference(conn, None, ref, utterance_id=said)
    pv.link_reference(conn, rid, ref)
    assert [(s["reference_id"], s["via"]) for s in evidence.sources_for_reason(conn, rid)] == [(ref, "reason")]


def test_the_words_of_a_personal_source_do_not_travel(conn, psg, plan):
    rid = _task(conn, psg)
    ref = pv.insert_reference(conn, project="proj", kind="chat", label="DM", occurred_at=CREATED, uri="chat://dm/1",
                              visibility="personal", said_by="a colleague", excerpt="between us: finance wants it out")
    pv.link_reference(conn, rid, ref)
    (src,) = evidence.sources_for_reason(conn, rid)
    assert src["excerpt"] is None and src["excerpt_withheld"] is True


def test_a_node_reason_reads_its_own_sources_and_its_quoted_words(conn, psg, plan, said):
    rid = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical",
                           verbatim=(said, 0, 20), recorded_by="agent")
    ref = _linked(conn)
    pv.link_reference(conn, None, ref, utterance_id=said)
    assert [s["via"] for s in evidence.sources_for_reason(conn, rid)] == ["quoted words"]


# ── (e) the window is this plan's, not the project's ────────────────────────

def test_window_is_the_plans_own_span_and_not_the_projects(conn, psg, plan):
    _task(conn, psg)
    assert [s["window"] for s in _slots(conn, psg)] == [[CREATED, COMPLETED]]


def test_an_open_plan_ends_its_window_now_rather_than_at_the_end_of_time(conn, psg, plan):
    _task(conn, psg)
    conn.execute("UPDATE Plans SET completed_at = NULL WHERE plan_id = 'P1'")
    conn.commit()
    now = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now')").fetchone()[0]
    (s,) = _slots(conn, psg)
    assert s["window"][0] == CREATED and CREATED < s["window"][1] <= now


# ── (f) not ranked; the cap ─────────────────────────────────────────────────

def test_slots_are_in_task_order_and_carry_no_significance(conn, psg, plan):
    pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_b", kind="organizational",
                     role="constraint", statement="Q3 excludes EMEA")
    out = reasons.fill_tasks(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg, bundle="one review",
                             tasks=[{"interpretation": "cents", "nodes": ["nk_c"]}, {"interpretation": "EMEA out"}])
    slots = _slots(conn, psg)
    assert [s["reason_id"] for s in slots] == out["reason_ids"]
    assert not any(k.startswith("significance") for s in slots for k in s)


def _three_tasks(conn, psg):
    reasons.fill_tasks(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg, bundle="one review",
                       tasks=[{"interpretation": "one", "nodes": ["nk_a"]}, {"interpretation": "two", "nodes": ["nk_b"]},
                              {"interpretation": "three"}])


def test_the_cap_is_PROVLEDGER_EVIDENCE_SLOTS_and_five_by_default(conn, psg, plan, monkeypatch):
    _three_tasks(conn, psg)
    assert evidence.DEFAULT_SLOTS == 5 and len(_slots(conn, psg)) == 3
    monkeypatch.setenv("PROVLEDGER_EVIDENCE_SLOTS", "2")
    assert [s["task"] for s in _slots(conn, psg)] == ["one", "two"]
    monkeypatch.setenv("PROVLEDGER_EVIDENCE_SLOTS", "0")
    assert _slots(conn, psg) == []
    monkeypatch.setenv("PROVLEDGER_EVIDENCE_SLOTS", "lots")
    assert len(_slots(conn, psg)) == 3


# ── (g) the hints are a rule, not a model ───────────────────────────────────

def test_hints_are_the_identifiers_of_the_root_and_the_task_then_the_nodes_names(conn, psg, plan, said):
    roots.record(conn, plan_id="P1", root={"kind": "new", "utterance_id": said})
    _task(conn, psg, interpretation="drop EMEA rows before the Q3 totals")
    (s,) = _slots(conn, psg)
    # the root's and the task's identifiers first — what is looked for is where
    # the cause was set — then each node's own name (sorted as the nodes are),
    # then the containers; `df` is under the three-letter floor
    assert s["hints"] == ["EMEA", "Q3", "clean_regions", "weekly_report", "amount", "rollup"], s["hints"]
    assert "excluded" not in s["hints"] and "Finance" not in s["hints"]


def test_hint_extraction_is_a_pure_function():
    args = (["pkg.rollup.weekly_report"], "EMEA excluded from the Q3 rollup")
    assert evidence.task_hints(*args) == ["EMEA", "Q3", "weekly_report", "rollup"]
    assert evidence.task_hints(*args) == evidence.task_hints(*args)
    assert evidence.hints_for("pkg.rollup.weekly_report", "EMEA excluded from the Q3 rollup") == \
        ["weekly_report", "rollup", "EMEA", "Q3"]


NO_MODEL = ("runner", "llm", "prompt", "judge", "claude", "openai")
FETCHERS = ("urlopen", "urllib", "requests", "httpx", "webfetch", "socket", "curl")


def test_nothing_in_the_slot_builder_calls_a_model_or_fetches_anything():
    src = (REPO / "orchestrator-backend" / "orchestrator" / "evidence.py").read_text(encoding="utf-8").lower()
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for word in NO_MODEL:
        assert word not in body, f"evidence slots must not reach for {word}"
    for f in FETCHERS:
        assert f not in src, f"provLedger must not reach for {f}: the host owns the credentials"


# ── (h) the command ─────────────────────────────────────────────────────────

def test_the_command_prints_the_task_slots_as_json(conn, psg, plan, registry, said):
    roots.record(conn, plan_id="P1", root={"kind": "new", "utterance_id": said})
    rid = _task(conn, psg)
    r = _cli(conn, "review", "evidence-slots", "--plan", "P1", "--json", env_extra={"PSG_REGISTRY_PATH": registry})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["plan_id"] == "P1" and out["project"] == "proj"
    (s,) = out["slots"]
    assert s["reason_id"] == rid and s["attach_to"] == {"utterance_id": said}
    assert f"--utterance {said}" in s["attach_with"] and "--excerpt" in s["attach_with"]
    assert f"--reason {rid}" in s["log_with"]


def test_the_command_prints_one_block_per_task(conn, psg, plan, registry):
    rid = _task(conn, psg)
    r = _cli(conn, "review", "evidence-slots", "--plan", "P1", env_extra={"PSG_REGISTRY_PATH": registry})
    assert r.returncode == 0, r.stderr
    assert f"task #{rid} (asserted)" in r.stdout and "EMEA excluded from the Q3 rollup" in r.stdout
    assert f"--reason {rid}" in r.stdout


def test_the_command_honours_the_cap(conn, psg, plan, registry):
    _three_tasks(conn, psg)
    r = _cli(conn, "review", "evidence-slots", "--plan", "P1", "--json",
             env_extra={"PSG_REGISTRY_PATH": registry, "PROVLEDGER_EVIDENCE_SLOTS": "1"})
    assert r.returncode == 0, r.stderr
    assert len(json.loads(r.stdout)["slots"]) == 1


def test_the_command_says_nothing_to_look_for_rather_than_failing(conn, psg, registry):
    db.insert_plan(conn, "P9", "nothing touched", created_at=CREATED, project="proj", project_source="declared")
    r = _cli(conn, "review", "evidence-slots", "--plan", "P9", env_extra={"PSG_REGISTRY_PATH": registry})
    assert r.returncode == 0, r.stderr
    assert "nothing to look for" in r.stdout


def test_a_plan_that_does_not_exist_is_refused_with_exit_2(conn, psg, registry):
    r = _cli(conn, "review", "evidence-slots", "--plan", "nope", "--json", env_extra={"PSG_REGISTRY_PATH": registry})
    assert r.returncode == 2, r.stdout
    assert "nope" in r.stderr, r.stderr


def test_the_command_writes_nothing(conn, psg, plan, registry):
    _task(conn, psg)
    tables = ("change_reason", "reference", "reference_link", "utterance", "evidence_log")
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables]
    r = _cli(conn, "review", "evidence-slots", "--plan", "P1", "--json", env_extra={"PSG_REGISTRY_PATH": registry})
    assert r.returncode == 0, r.stderr
    assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables] == before
