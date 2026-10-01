"""`provledger plan <id>` — a plan's steps and its deviations (FL-155).

Nothing read Plans / Steps / Deviations. `metrics plan` gives cost ratios,
`review evidence-log` gives evidence slots, and the dashboard's /plan/{id} is
HTML over HTTP — useless to a model in a terminal. So the cause of a change
could be recorded precisely, with a number, and still be reachable only by
hand-written SQL.

The case that settled it: "why is the review timeout 4800 seconds?" The answer
is on record — the previous 300s ceiling was breached at 300.1s — in the
`failure_reason` of a step whose plan closed COMPLETED, because the failure was
recovered. So the two properties the fixture below reproduces are the whole
point of the read:

  1. a FAILED step inside a COMPLETED plan — the plan's own status hides it;
  2. that step is a recovery sub-step nested two deep, so a flat list of steps
     loses the thing that explains what happened.
"""
import pytest

from orchestrator import plan_read

PLAN = "dp6-a-20260927063613"
TIMEOUT_REASON = ("graph refresh (init_project.sh) timed out: exceeded the 300s ceiling (elapsed 300.1s). "
                  "A timeout is a failure, not a pass; raise the ceiling on the command line "
                  "(--timeout-graph / --timeout-tests) only if it is genuinely too low.")
PROCESS_REASON = ("Not a failure of the work — a failure of this step, caused by me. I ran start-step on it by hand "
                  "and then tried to complete it with the results of suites I had already run elsewhere. The store "
                  "refused, because a COMMAND step completes only through run-step.sh, which captures the exit code "
                  "itself.")
LONG_LOG = "step log line\n" * 900


def _step(conn, sid, *, parent=None, status="COMPLETED", order=0, depth=0, desc="do the thing",
          stype="CODE", log="", failure=None, summary=None, review=0):
    conn.execute("INSERT INTO Steps (step_id, plan_id, parent_step_id, description, status, execution_order, "
                 "depth_level, log_context, step_type, failure_reason, summary, is_review) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (sid, PLAN, parent, desc, status, order, depth, log, stype, failure, summary, review))


@pytest.fixture
def seeded(conn):
    """The live plan's shape: COMPLETED, with a failed top-level step and a
    failed review attempt recovered by a sub-step two levels down."""
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, revision_count, max_revisions, created_at, "
                 "completed_at, project, project_source, review_state, user_query) "
                 "VALUES (?, 'evidence foundations', 'COMPLETED', 7, 8, '2026-09-27 06:36:13', "
                 "'2026-09-30 18:35:42', 'prov_ledger', 'declared', 'reviewed', 'answer without git history')",
                 (PLAN,))
    _step(conn, f"{PLAN}-A", order=0, log="analysis log", summary="read the spec")
    _step(conn, f"{PLAN}-S", order=18, status="FAILED", stype="COMMAND", failure=PROCESS_REASON, log=LONG_LOG)
    _step(conn, f"{PLAN}-S.1", parent=f"{PLAN}-S", order=1800, depth=1, summary="hardened the chain")
    _step(conn, f"{PLAN}-REVIEW", order=19, stype=None, review=1, desc="review")
    _step(conn, f"{PLAN}-REVIEW.1", parent=f"{PLAN}-REVIEW", order=1900, depth=1, status="FAILED",
          stype="SUB_AGENT", failure="working tree has uncommitted changes: ['INSTALL.md']")
    _step(conn, f"{PLAN}-REVIEW.1.1", parent=f"{PLAN}-REVIEW.1", order=190000, depth=2, status="FAILED",
          stype=None, failure=TIMEOUT_REASON, log="gate 1 ok\ngate 2 ok\n")
    _step(conn, f"{PLAN}-REVIEW.1.1.1", parent=f"{PLAN}-REVIEW.1.1", order=19000000, depth=3,
          stype="ANALYSIS", summary="redone and carried through")
    conn.execute("INSERT INTO Deviations (plan_id, target_step_id, justification, new_step_ids, revision_count, created_at) "
                 "VALUES (?, ?, ?, ?, 6, '2026-09-29 04:44:01')",
                 (PLAN, f"{PLAN}-REVIEW.1",
                  "REVIEW.1.1 FAILED on this plan's own new --timeout-graph default of 300s. That ceiling is below "
                  "every refresh this project has ever recorded. Re-running with --timeout-graph 4500.",
                  '["%s-REVIEW.1.2"]' % PLAN))
    conn.commit()
    return conn


def test_a_failed_step_is_surfaced_even_though_the_plan_closed_completed(conn, seeded):
    """Property 1. A read that only shows failures for FAILED plans misses the
    entire case this exists for: the detour is invisible from the status."""
    doc = plan_read.plan(conn, PLAN)
    assert doc["status"] == "COMPLETED"
    assert doc["counts"]["failed"] == 3 and doc["counts"]["steps"] == 7
    assert doc["failed"] == [f"{PLAN}-S", f"{PLAN}-REVIEW.1", f"{PLAN}-REVIEW.1.1"]
    head = plan_read.render(doc).splitlines()
    assert any("3 failed" in ln for ln in head[:4]), head[:4]


def test_the_parent_child_nesting_is_legible_and_recovery_reads_in_order(conn, seeded):
    """Property 2. A flat list loses which attempt a retry retries."""
    doc = plan_read.plan(conn, PLAN)
    assert [(s["step_id"].split("-")[-1], s["depth"]) for s in doc["steps"]] == [
        ("A", 0), ("S", 0), ("S.1", 1), ("REVIEW", 0), ("REVIEW.1", 1), ("REVIEW.1.1", 2), ("REVIEW.1.1.1", 3)]
    text = plan_read.render(doc)
    deep = next(ln for ln in text.splitlines() if "REVIEW.1.1 " in ln or ln.strip().startswith(f"{PLAN}-REVIEW.1.1 "))
    assert deep.startswith("      "), repr(deep)             # depth 2 is indented, not flattened


def test_the_failure_reason_that_holds_the_number_is_printed_whole(conn, seeded):
    """The 300s ceiling and the 300.1s that breached it are the answer; a cut
    here would lose the number, which is the only reason the read exists."""
    doc = plan_read.plan(conn, PLAN)
    step = next(s for s in doc["steps"] if s["step_id"].endswith("REVIEW.1.1"))
    assert step["failure_reason"] == TIMEOUT_REASON
    text = plan_read.render(doc)
    assert "exceeded the 300s ceiling (elapsed 300.1s)" in text
    assert "only if it is genuinely too low." in text        # to the last character, including the tail


def test_failure_reason_carries_machine_output_and_written_accounts_alike(conn, seeded):
    """-S's reason is a person explaining a process violation; REVIEW.1.1's is a
    timer's output. Neither is formatted as if it were the other."""
    doc = plan_read.plan(conn, PLAN)
    text = plan_read.render(doc)
    assert PROCESS_REASON in text and TIMEOUT_REASON in text


def test_the_deviations_say_what_was_changed_and_against_which_step(conn, seeded):
    doc = plan_read.plan(conn, PLAN)
    assert len(doc["deviations"]) == 1
    d = doc["deviations"][0]
    assert d["target_step_id"] == f"{PLAN}-REVIEW.1" and d["new_step_ids"] == [f"{PLAN}-REVIEW.1.2"]
    assert "--timeout-graph 4500" in d["justification"]
    assert "--timeout-graph 4500" in plan_read.render(doc)


def test_a_bounded_log_says_how_much_it_cut_and_the_command_that_reads_it_whole(conn, seeded):
    """FL-154 again, one layer up: log_context runs to 16 KiB, so it is bounded
    — and a bound that does not announce itself is the defect being fixed."""
    doc = plan_read.plan(conn, PLAN)
    s = next(x for x in doc["steps"] if x["step_id"] == f"{PLAN}-S")
    assert len(s["log_context"]) == plan_read.CAP_LOG
    assert s["log_chars"] == len(LONG_LOG) and s["log_cut"] == len(LONG_LOG) - plan_read.CAP_LOG
    text = plan_read.render(doc)
    assert f"+{s['log_cut']} chars cut" in text
    assert f"provledger plan {PLAN} --step {PLAN}-S --full" in text
    whole = plan_read.plan(conn, PLAN, log_chars=0)
    s2 = next(x for x in whole["steps"] if x["step_id"] == f"{PLAN}-S")
    assert s2["log_context"] == LONG_LOG and s2["log_cut"] == 0


def test_one_step_can_be_read_on_its_own(conn, seeded):
    doc = plan_read.plan(conn, PLAN, step=f"{PLAN}-REVIEW.1.1")
    assert [s["step_id"] for s in doc["steps"]] == [f"{PLAN}-REVIEW.1.1"]
    assert doc["step"] == f"{PLAN}-REVIEW.1.1" and doc["counts"]["steps"] == 7
    assert TIMEOUT_REASON in plan_read.render(doc)
    assert doc["steps"][0]["parent_step_id"] == f"{PLAN}-REVIEW.1"      # still says what it hangs under


def test_an_unknown_plan_and_an_unknown_step_say_so(conn, seeded):
    assert plan_read.plan(conn, "no-such-plan") is None
    doc = plan_read.plan(conn, PLAN, step="no-such-step")
    assert doc["steps"] == [] and "no-such-step" in plan_read.render(doc)


def test_the_plan_read_writes_nothing_at_all(conn, seeded):
    tables = ("Plans", "Steps", "Deviations", "change_reason", "read_hit", "influence")
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables]
    plan_read.plan(conn, PLAN)
    plan_read.plan(conn, PLAN, log_chars=0)
    plan_read.plan(conn, PLAN, step=f"{PLAN}-S")
    after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables]
    assert before == after and conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0] == 0


# ── the CLI entry ────────────────────────────────────────────────────────────

def test_cli_plan_reaches_the_failed_step_inside_a_completed_plan(conn, seeded):
    import os
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(repo / "orchestrator-backend"))

    def cli(*argv):
        return subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True,
                              env=env, cwd=str(repo))
    r = cli("plan", PLAN)
    assert r.returncode == 0, r.stderr
    assert "COMPLETED" in r.stdout and "3 failed" in r.stdout
    assert "exceeded the 300s ceiling (elapsed 300.1s)" in r.stdout
    assert "--timeout-graph 4500" in r.stdout
    one = cli("plan", PLAN, "--step", f"{PLAN}-REVIEW.1.1")
    assert one.returncode == 0 and TIMEOUT_REASON in one.stdout
    full = cli("plan", PLAN, "--step", f"{PLAN}-S", "--full")
    assert full.returncode == 0 and full.stdout.count("step log line") == 900
    j = cli("plan", PLAN, "--json")
    assert j.returncode == 0 and '"deviations"' in j.stdout and '"failed"' in j.stdout
    missing = cli("plan", "no-such-plan")
    assert missing.returncode == 1 and "no-such-plan" in (missing.stdout + missing.stderr)
    assert conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0] == 0


def test_each_field_of_a_step_is_printed_exactly_once(conn, seeded):
    """Caught on the live plan: `description` is rendered on its own line AND
    walked by the bounded-field loop, so every one of 29 steps printed its
    instruction twice — doubling the read whose whole point is to be readable."""
    doc = plan_read.plan(conn, PLAN)
    lines = plan_read.render(doc).splitlines()
    assert sum(1 for ln in lines if ln.strip() == "do the thing") == 6       # one per step that has that description
    assert not [ln for ln in lines if ln.strip().startswith("description: ")]


def test_a_deviation_is_not_printed_as_a_bare_cite_because_that_number_means_a_record(conn, seeded):
    """The worst defect found in a day of running this: a silent wrong answer in
    the one place the tool is supposed to be unfalsifiable.

    `plan dp2d-t0-20260916150040` printed its deviations as `#92 #93 #94`, and
    `provledger record '#94'` returned an unrelated `stated` reason from a different
    plan — no error, no warning, same `#N` spelling, two namespaces. Someone handed
    "deviation #94" as evidence and told to check it with `record` gets a different
    row and nothing saying anything went wrong. They then either believe the wrong
    thing or conclude the evidence was invented.

    The cite namespace already separates kinds by letter — `#r3` a source, `#i4` an
    influence, `#x6` an expectation, `#m8` a measured value. Deviations never got
    one. They get `#v`.
    """
    doc = plan_read.plan(conn, PLAN)
    assert doc["deviations"], "the fixture must carry a deviation for this to mean anything"
    did = doc["deviations"][0]["deviation_id"]
    out = plan_read.render(doc)
    assert f"#v{did}" in out, "a deviation must carry its own letter, like every other kind"
    assert f"#{did} ·" not in out, \
        f"a bare #{did} is how a ledger record is written, and `record '#{did}'` resolves to one"
