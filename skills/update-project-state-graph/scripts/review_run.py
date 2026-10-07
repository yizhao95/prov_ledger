#!/usr/bin/env python3
"""review_run.py — deterministic, injectable driver for the update-project-state-graph
review (SKILL.md steps 0–4c). The agent no longer walks the flow by hand: it
chooses the test command, decides a signature override and supplies reasons.

    python3 review_run.py --plan-id P --project X [--registry projects.json]
        [--reasons stub|unstated|ask|<file.json>]   # stub: "scenario: <event_types>" per slot;
                                                # unstated: every slot NULL;
                                                # ask: print the checklist, exit 6, resume later;
                                                # json: [{node_key|qualified_name, text}, ...]
        [--tests "<command>"]                   # 4b re-test, run in the repo; absent -> logged as skipped
        [--timeout-tests 600] [--timeout-graph 4800] # seconds; a breach FAILS the step (write scripts: 60s)
        [--accept-signature "<reason>"]         # may override ONLY the signature gate; reason is logged
        [--accept-stale "<reason>"]             # may override ONLY the stale_references gate (moved + re-exported defs)
        [--allow-dirty]                         # refresh despite uncommitted tracked changes (traced); default: FAIL
        [--as-recovery <step>]                  # an attempt FAILED: run 1–4c as that PENDING retry, which
                                                # hangs UNDER the attempt it retries (REVIEW.1.1, REVIEW.1.1.1, …)
        [--dry-run]                             # steps 0–3 only, nothing written; the report NAMES the 4b/4c
                                                # gates it did not evaluate
        [--json]                                # machine-readable result on stdout (last line)

Flow: 0 lock registered_sha (append-log on <plan>-REVIEW, then start-step
<plan>-REVIEW.1) · 1–3 resolve_range / changed_symbols / full_verdict ·
4a gaps -> fail-step REVIEW.1 (exit 1) · 4b refresh (init_project.sh with
PROVLEDGER_* attribution) -> --tests -> selfcheck · 4c reason-slots ->
reason-fill -> complete-step REVIEW.1 (exit 0).

Every write goes through the executing-plans shell scripts (single write path).
If REVIEW.1 is already IN_PROGRESS the driver resumes: at 4c when the registry
already points at HEAD (a previous run stopped in 4c, e.g. exit 6 on an unknown
reasons key), otherwise from step 1 (a previous run died before its refresh).

Exit codes: 0 the plan closed COMPLETED · 1 it did not — closed FAILED (a refresh
/ tests timeout closes FAILED here too), left short of COMPLETED by a close-time
refusal or an unfinished sibling recovery step, or a failing --dry-run verdict ·
5 a write script failed (including its 60s timeout) · 6 bad input (unknown
project / reasons key / file, or an --as-recovery step of the wrong shape).
Exit 0 never means anything but "this plan is COMPLETED".

The refresh and the test command run in their own process group, and a timeout
kills the group (see `_run_group`): `init_project.sh` spawns the analyzer, and an
analyzer that outlived its ceiling would keep writing the graph the next run
reads as its predecessor.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parents[1]
EXEC = SKILLS / "executing-plans" / "scripts"
PSG_SCRIPTS = SKILLS / "project-state-graph" / "scripts"
INIT_PROJECT = PSG_SCRIPTS / "init_project.sh"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PSG_SCRIPTS))
import review_diff  # noqa: E402
import selfcheck  # noqa: E402

DEFAULT_REGISTRY = os.path.expanduser("~/skill-workspace/project-graphs/projects.json")
DEFAULT_ORCH_DB = os.path.expanduser("~/skill-workspace/orchestrator.db")

# A4: every subprocess this driver starts has a ceiling, and the ceilings are
# published in SKILL.md next to these numbers. The review is minutes long by
# design — it rebuilds the whole graph and runs the project's own suite — but
# "minutes" must have an end: a tool that gets slower in silence is the thing
# this project exists to prevent. A breach therefore takes the same path as a
# non-zero exit (fail-step with the elapsed seconds in the reason), never a
# hang that leaves <plan>-REVIEW.1 IN_PROGRESS with nothing written.
TIMEOUT_WRITE_S = 60             # one executing-plans write script (a single sqlite write)
DEFAULT_TIMEOUT_GRAPH_S = 4800  # init_project.sh: a full re-analysis of the repo.
#   Read off this project's own analysis_run table rather than guessed: ten runs
#   at 795-3085s, median 869s, on a 6710-node graph. The first value here was 300s,
#   which was below every refresh this repository has ever recorded and auto-failed
#   the review the first time it ran. A ceiling is meant to catch a hang, not a
#   large repository, so it sits well above the slowest observed run.
DEFAULT_TIMEOUT_TESTS_S = 600    # --tests: the project's own test suite


# FL-134: --dry-run stops after step 3, so its verdict covers the gates of steps
# 1–3 and nothing else. The gates below are the ones it cannot reach, and the run
# names every one of them — in the report and in --json. A dirty working tree
# auto-failed a real plan on the gate at 4b immediately after a dry run had said
# PASS: a pre-flight that stays silent about what it did not look at is worse than
# no pre-flight, because the silence reads as a clean bill of health.
DRY_RUN_NOT_EVALUATED = (
    ("dirty_working_tree", "4b", "uncommitted changes to tracked files (FAILs the run unless --allow-dirty)"),
    ("graph_refresh", "4b", "the init_project.sh re-analysis, and its --timeout-graph ceiling"),
    ("tests", "4b", "the --tests command in the repo, and its --timeout-tests ceiling"),
    ("selfcheck", "4b", "the hard invariants on the refreshed graph"),
    ("close_reasons", "4c", "reason-slots / reason-fill and the close itself"),
)


def _killpg(pid: int) -> None:
    """SIGKILL the process group `pid` leads, and never raise: by the time we get
    here the step is already failing, and a group that has gone on its own is the
    outcome we wanted anyway."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def _run_group(args, **kw):
    """`subprocess.run`, but the ceiling takes the whole process tree down.

    `subprocess.run(timeout=...)` kills only the process it started. The graph
    refresh starts `init_project.sh`, which spawns the analyzer — so a refresh that
    hit its ceiling used to leave that analyzer running and still writing to the
    state-graph database. The step failed, loudly and correctly; the database was
    the problem. A half-written graph becomes the PREDECESSOR the next run matches
    identities against, which is how `nk_` keys get inherited.

    So `start_new_session=True` makes the child the leader of its own process group
    (pgid == its pid) and a TimeoutExpired SIGKILLs that whole group before it
    propagates to the caller, which still fails the step with the ceiling and the
    elapsed seconds in the reason.

    This is the bleeding stopped, not the wound closed: it kills the writer, it does
    not make the bytes already written safe. The real fix is a completion marker on
    the analyzer's write path so an interrupted run is DISCARDED rather than trusted
    on the next pass. That changes how the analyzer writes and deserves its own
    step; it is deliberately not attempted here.

    The Popen shim is only how the pid is learned: `subprocess.run` reaps the child
    before it raises and TimeoutExpired carries no pid. The call itself still goes
    through the module attribute `subprocess.run`, which is the single seam the
    tests replace.
    """
    kw["start_new_session"] = True
    leaders: list[int] = []
    real_popen = subprocess.Popen

    class _Tracked(real_popen):                      # type: ignore[misc,valid-type]
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            leaders.append(self.pid)                 # start_new_session => pid is the pgid

    subprocess.Popen = _Tracked
    try:
        return subprocess.run(args, **kw)
    except subprocess.TimeoutExpired:
        for pid in leaders:
            _killpg(pid)
        raise
    finally:
        subprocess.Popen = real_popen


def _die(msg: str, code: int) -> None:
    print(f"❌ review_run: {msg}", file=sys.stderr)
    sys.exit(code)


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Driver:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.plan = a.plan_id
        self.project = a.project
        self.review_step = f"{self.plan}-REVIEW"
        self.child = f"{self.plan}-REVIEW.1"
        self.registry = a.registry or os.environ.get("PSG_REGISTRY_PATH") or DEFAULT_REGISTRY
        self.env = dict(os.environ, PSG_REGISTRY_PATH=self.registry)
        self.orch_db = os.environ.get("ORCH_DB", DEFAULT_ORCH_DB)
        self.tmp = Path(tempfile.mkdtemp(prefix="review_run-"))
        self.log: list[str] = []
        self.manual: list[str] = []
        self.result: dict = {"plan_id": self.plan, "project": self.project, "verdict": None, "gates": {},
                             "range": None, "refreshed_sha": None, "slots": 0, "filled": 0,
                             "unstated": 0, "closed": None}

    # ── plumbing ─────────────────────────────────────────────────────────────
    def say(self, line: str) -> None:
        self.log.append(line)
        if not self.a.json:
            print(line)

    def op(self, script: str, payload: dict, ok_codes=(0,)) -> tuple[int, dict, str]:
        """Run executing-plans/scripts/<script>.sh with a JSON input file; return
        (exit_code, last-json-line, combined output)."""
        f = self.tmp / f"{script}-{payload.get('step_id') or payload.get('plan_id')}.json"
        f.write_text(json.dumps(payload, ensure_ascii=False))
        t0 = time.monotonic()
        try:
            p = subprocess.run(["bash", str(EXEC / f"{script}.sh"), str(f)], env=self.env,
                               capture_output=True, text=True, timeout=TIMEOUT_WRITE_S)
        except subprocess.TimeoutExpired:
            # Exit 5 (a write script failed) — the same path as a non-zero exit. What
            # the ledger shows afterwards is read back and said (FL-211): a
            # complete-step can have written its step and then been cut inside the
            # plan close, and "nothing confirmed" was false then.
            _die(f"{script}.sh timed out: exceeded the {TIMEOUT_WRITE_S}s ceiling "
                 f"(elapsed {time.monotonic() - t0:.1f}s); {self.state_after(payload)}", 5)
        # stdout = the op's result (pretty JSON, multi-line) followed by ONE
        # single-line OK marker {"ok":true,"op":...}. Parse both.
        lines = p.stdout.strip().splitlines()
        marker: dict = {}
        body: dict = {}
        if lines:
            try:
                marker = json.loads(lines[-1])
            except ValueError:
                marker = {}
            try:
                body = json.loads("\n".join(lines[:-1])) if len(lines) > 1 else {}
            except ValueError:
                body = {}
        if p.returncode not in ok_codes:
            _die(f"{script}.sh exited {p.returncode}: {(p.stderr or p.stdout)[-800:]}", 5)
        return p.returncode, {**body, "_marker": marker}, p.stdout + p.stderr

    def state_after(self, payload: dict) -> str:
        """What the ledger shows for a write that outlived its ceiling: the step it
        named and the plan, as they read now. Observations only."""
        step_id = payload.get("step_id")
        try:
            step = self.read("SELECT status FROM Steps WHERE step_id = ?", step_id) if step_id else []
            plan = self.read("SELECT status, review_state FROM Plans WHERE plan_id = ?", self.a.plan_id)
        except Exception as exc:                           # the ledger itself could not be read
            return f"afterwards the ledger could not be read ({type(exc).__name__}: {exc})"
        step_status = step[0][0] if step and step[0] else None
        plan_status = plan[0][0] if plan and plan[0] else None
        plan_review = plan[0][1] if plan and len(plan[0]) > 1 else None
        said = []
        if step_id:
            said.append(f"{step_id} reads {step_status or 'absent'}")
        if plan_status:
            said.append(f"plan {self.a.plan_id} reads {plan_status} ({plan_review or 'no review state'})")
        text = "afterwards " + "; ".join(said) if said else "afterwards nothing could be read back"
        if step_status == "COMPLETED" and plan_status and plan_status != "COMPLETED" and str(step_id).endswith("REVIEW.1"):
            text += " — the step was written, the close did not finish"
        return text

    def read(self, sql: str, *params):
        c = sqlite3.connect(self.orch_db)
        try:
            return c.execute(sql, params).fetchall()
        finally:
            c.close()

    def registry_entry(self) -> dict | None:
        try:
            reg = json.loads(Path(self.registry).read_text())
        except (OSError, ValueError):
            return None
        return next((p for p in reg.get("projects", []) if p.get("name") == self.project), None)

    def dry_run_not_evaluated(self) -> None:
        """Name every gate this run did not evaluate (FL-134). Called on both dry-run
        exits, PASS and FAIL alike: the point is not the verdict but its scope."""
        names = [g for g, _step, _what in DRY_RUN_NOT_EVALUATED]
        self.result["dry_run"] = True
        self.result["not_evaluated"] = names
        self.say(f"[dry-run] {len(names)} gate(s) NOT evaluated by this run — a dry-run verdict "
                 f"covers steps 0–3 only and is NOT a real-run verdict:")
        for gate, step, what in DRY_RUN_NOT_EVALUATED:
            self.say(f"[dry-run]   {gate} ({step}): {what}")

    def recovery_chain(self, step_id: str) -> list[str] | None:
        """`step_id` plus its ancestors up to <plan>-REVIEW.1 (nearest first), or
        None when the step is neither REVIEW.1 nor one of its descendants."""
        chain: list[str] = []
        cur: str | None = step_id
        while cur and len(chain) <= 16:              # 16: far past the depth breaker; a loop cannot spin here
            chain.append(cur)
            if cur == f"{self.plan}-REVIEW.1":
                return chain
            rows = self.read("SELECT parent_step_id FROM Steps WHERE step_id = ?", cur)
            cur = rows[0][0] if rows else None
        return None

    def recovered(self, step_id: str) -> bool:
        """Read-only mirror of api._is_step_recovered, for the sibling guard below:
        COMPLETED, or FAILED with every non-review child recovered (recursively)."""
        rows = self.read("SELECT status FROM Steps WHERE step_id = ?", step_id)
        if not rows:
            return False
        if rows[0][0] == "COMPLETED":
            return True
        if rows[0][0] != "FAILED":
            return False
        kids = self.read("SELECT step_id FROM Steps WHERE parent_step_id = ? AND is_review = 0", step_id)
        return bool(kids) and all(self.recovered(k[0]) for k in kids)

    def validated_recovery(self, sub_id: str) -> str:
        """The step this run may execute as (FL-030, FL-138), or exit 6.

        A retry is a CHILD of the attempt it retries — REVIEW.1.1, then
        REVIEW.1.1.1 — never a sibling of it. The rollup this feeds
        (api._is_step_recovered) asks whether EVERY sub-task came through, so a
        FAILED sibling outvotes a COMPLETED one for ever: the review never
        reopens and the plan cannot close. Nesting makes the existing recursion
        true of retries as well, and introduces no new concept.
        """
        review1 = f"{self.plan}-REVIEW.1"
        rows = self.read("SELECT status, parent_step_id FROM Steps WHERE step_id = ?", sub_id)
        if not rows:
            _die(f"--as-recovery {sub_id}: no such step. Deviate under the FAILED attempt first — the retry is "
                 f"its child ({review1}.1 for the first retry, {review1}.1.1 for the next)", 6)
        status, parent = rows[0]
        if status not in ("PENDING", "IN_PROGRESS"):
            _die(f"--as-recovery {sub_id}: must be PENDING (or IN_PROGRESS, to resume at 4c); got {status}", 6)
        if not parent or self.recovery_chain(parent) is None:
            _die(f"--as-recovery {sub_id}: its parent ({parent}) is neither {review1} nor one of {review1}'s "
                 f"attempts — a review retry hangs under the attempt it retries", 6)
        prow = self.read("SELECT status FROM Steps WHERE step_id = ?", parent)
        if not prow or prow[0][0] != "FAILED":
            _die(f"--as-recovery {sub_id}: the attempt it retries ({parent}) must be FAILED; "
                 f"got {prow[0][0] if prow else None}", 6)
        stuck = [sid for sid, st in
                 self.read("SELECT step_id, status FROM Steps WHERE parent_step_id = ? AND is_review = 0", parent)
                 if sid != sub_id and st == "FAILED" and not self.recovered(sid)]
        if stuck:
            _die(f"--as-recovery {sub_id}: it is a SIBLING of the unrecovered attempt(s) {stuck} under {parent}. "
                 f"A FAILED sibling outvotes a COMPLETED one in the recovery rollup, so this retry could never "
                 f"close the plan. Deviate under {stuck[-1]} instead and run as that child", 6)
        return sub_id

    def finish(self, exit_code: int) -> None:
        if self.a.json:
            print(json.dumps(self.result, ensure_ascii=False))
        sys.exit(exit_code)

    # ── closes ───────────────────────────────────────────────────────────────
    @staticmethod
    def timeout_reason(what: str, limit: float, elapsed: float) -> str:
        """The reason a timeout writes into fail-step. It names the ceiling and the
        elapsed seconds so the record answers "why did this stop?" without anyone
        re-running it — silence is the failure mode this guards against."""
        return (f"{what} timed out: exceeded the {limit:g}s ceiling (elapsed {elapsed:.1f}s). "
                f"A timeout is a failure, not a pass; raise the ceiling on the command line "
                f"(--timeout-graph / --timeout-tests) only if it is genuinely too low.")

    def fail(self, reason: str) -> None:
        self.say(f"[4a] FAIL — {reason}")
        self.op("fail-step", {"step_id": self.child, "reason": reason[:500], "log_context": "\n".join(self.log)})
        self.result["closed"] = "FAILED"
        self.finish(1)

    # ── the flow ─────────────────────────────────────────────────────────────
    def run(self) -> None:
        a = self.a
        entry = self.registry_entry()
        if entry is None:
            _die(f"project {self.project!r} is not in the registry {self.registry}", 6)
        registered_sha = entry.get("commit_sha") or ""
        repo = entry["repo"]
        db_path = entry.get("db_path")
        if a.as_recovery:
            # FL-030: an attempt FAILED and the agent hung a retry under it — run
            # 1–4c AS that retry (start/complete it); the FL-019 close then sees a
            # refreshed registry and stated reasons. FL-138: the retry is a child
            # of the attempt it retries, so each retry costs one depth level and
            # the depth_level<=3 breaker allows REVIEW.1.1 and REVIEW.1.1.1.
            self.child = self.validated_recovery(a.as_recovery)
            self.say(f"[recovery] running the review as {self.child}")
        rows = self.read("SELECT status FROM Steps WHERE step_id = ?", self.child)
        child_status = rows[0][0] if rows else None
        if child_status is None:
            _die(f"{self.child} does not exist — the plan is not in NEEDS_REVIEW", 6)
        head = review_diff._git(repo, "rev-parse", "HEAD").strip()

        resumed = child_status == "IN_PROGRESS" and not a.dry_run
        if resumed and registered_sha == head:
            self.say(f"[resume] {self.child} is IN_PROGRESS and the registry is at HEAD — continuing at 4c (no second refresh)")
            self.result["refreshed_sha"] = registered_sha
            return self.close_with_reasons()
        if resumed:
            # the previous run died between start-step and the end of the refresh
            # (killed process): the lock line and start-step already exist, the
            # refresh does not — redo 1–4b, never skip to 4c.
            self.say(f"[resume] {self.child} is IN_PROGRESS but the registry ({registered_sha[:10]}) is behind "
                     f"HEAD ({head[:10]}): the previous run died before the refresh completed — redoing 1–4b")

        # 0. lock the registered sha BEFORE anything else
        lock = (f"[REVIEW LOCK] registered_sha={registered_sha} repo={repo} head={head} "
                f"registry_updated_at={entry.get('updated_at')} locked_at={_utc()}")
        self.say(lock)
        if not a.dry_run and not resumed:
            self.op("append-log", {"step_id": self.review_step, "text": lock})
            start = {"step_id": self.child, "log_context": lock,
                     "agent_input": f"review_run.py --plan-id {self.plan} --project {self.project}"
                                    f" --reasons {a.reasons} --tests {a.tests!r}"
                                    f" --timeout-tests {a.timeout_tests:g} --timeout-graph {a.timeout_graph:g}"}
            if not a.as_recovery:
                start["type"] = "SUB_AGENT"        # a recovery sub-step keeps its declared type
            self.op("start-step", start)
        if not db_path or not os.path.exists(db_path):
            reason = f"no deep graph for {self.project!r} (db_path={db_path}) — run project-state-graph first"
            if a.dry_run:
                self.say(reason)
                self.dry_run_not_evaluated()
                self.finish(1)
            self.fail(reason)

        # 1–3. range -> changed symbols -> combined verdict
        base, head_ref, mode = review_diff.resolve_range(repo, registered_sha)
        changed = review_diff.changed_symbols(repo, base, head_ref)
        files = review_diff.contract_diff.changed_files(repo, base, head_ref)
        verdict = review_diff.full_verdict(db_path, repo, base, head_ref, changed=changed,
                                           registered_sha=registered_sha)
        self.result["range"] = {"base": base, "head": head_ref, "mode": mode, "files": files}
        self.result["gates"] = verdict["gates"]
        self.say(f"[1] resolve_range -> {mode} {base[:10]}..{head_ref} ({len(files)} files)")
        self.say(f"[2] changed_symbols: {[(c.get('kind'), c.get('old_name') or c.get('name')) for c in changed]}")
        self.say("[3] " + verdict["text"])
        ok = verdict["ok"]
        accepted = {g: r for g, r in (("signature", a.accept_signature), ("stale_references", a.accept_stale)) if r}
        if not ok and accepted:
            failing = [g for g, v in verdict["gates"].items() if not v]
            if failing and all(g in accepted for g in failing):
                ok = True
                for g in failing:
                    self.manual.append(f"[MANUAL VERDICT] {g} gate overridden: {accepted[g]}")
                    self.say(self.manual[-1])
            else:
                self.say(f"[MANUAL VERDICT] override ignored: failing gates {failing} are not all covered by "
                         f"{sorted(accepted)} — only the signature and stale_references gates can be accepted")
        self.result["verdict"] = ok
        if a.dry_run:
            self.say(f"[dry-run] verdict {'PASS' if ok else 'FAIL'} on steps 0–3 ONLY; nothing written")
            self.dry_run_not_evaluated()
            self.finish(0 if ok else 1)
        if not ok:
            gaps = {g: v for g, v in verdict["gaps"].items() if not verdict["gates"].get(g, True)}
            self.fail(f"gates failed {[g for g in gaps]}: {json.dumps(gaps, ensure_ascii=False)[:1500]}")

        # 4b (guard, phase 5): the refresh analyses the WORKING TREE. Tracked
        # files with uncommitted changes would put unreviewed code into the
        # graph — refuse unless the agent takes it on the record.
        dirty = review_diff._git(repo, "status", "--porcelain", "--untracked-files=no").rstrip("\n")
        if dirty.strip():
            files = sorted({ln.split(maxsplit=1)[1] for ln in dirty.splitlines() if ln.strip()})
            if not a.allow_dirty:
                self.fail(f"working tree has uncommitted changes: {files}; commit/stash them or pass --allow-dirty")
            self.manual.append(f"[MANUAL VERDICT] refresh on dirty tree: {files}")
            self.say(self.manual[-1])
        env = dict(self.env, PROVLEDGER_PLAN_ID=self.plan, PROVLEDGER_STEP_ID=self.child, PROVLEDGER_TRIGGER="review")
        t0 = time.monotonic()
        try:
            # FL-084: refresh the graph where the project is registered. Without
            # --out-dir a project registered elsewhere got a new graph with no
            # history in the default directory, and the registry followed it.
            out_dir = ["--out-dir", str(Path(entry["db_path"]).parent)] if entry.get("db_path") else []
            p = _run_group(["bash", str(INIT_PROJECT), "--name", self.project, "--repo", repo, *out_dir],
                           env=env, capture_output=True, text=True, timeout=a.timeout_graph)
        except subprocess.TimeoutExpired:
            self.fail(self.timeout_reason("graph refresh (init_project.sh)", a.timeout_graph,
                                          time.monotonic() - t0))
        out_lines = (p.stdout + p.stderr).strip().splitlines()
        self.say(f"[4b] init_project.sh exit={p.returncode} ({len(out_lines)} lines)\n" + "\n".join(out_lines[-3:]))
        if not a.json:
            print("\n".join(out_lines))
        if p.returncode != 0:
            self.fail(f"graph refresh failed (init_project.sh exit {p.returncode})")
        entry = self.registry_entry() or entry
        db_path = entry.get("db_path") or db_path
        self.result["refreshed_sha"] = entry.get("commit_sha")
        if a.tests:
            t0 = time.monotonic()
            try:
                t = _run_group(a.tests, shell=True, cwd=repo, capture_output=True, text=True,
                               timeout=a.timeout_tests)
            except subprocess.TimeoutExpired:
                self.fail(self.timeout_reason(f"tests `{a.tests}`", a.timeout_tests, time.monotonic() - t0))
            t_lines = (t.stdout + t.stderr).strip().splitlines()
            self.say(f"[4b] tests `{a.tests}` exit={t.returncode} ({len(t_lines)} lines)\n" + "\n".join(t_lines[-5:]))
            if t.returncode != 0:
                self.fail(f"tests failed (exit {t.returncode}): {a.tests}")
        else:
            self.say("[4b] tests skipped: no --tests command given")
        sc = selfcheck.run(db_path)
        sc_lines = sc["report"].splitlines()
        self.say("[4b] " + "\n".join([sc_lines[0]] + [ln for ln in sc_lines[1:] if "[OK  ]" not in ln]))
        if not sc["ok"]:
            self.fail("selfcheck failed on the refreshed graph (hard invariant)")
        self.close_with_reasons()

    def close_with_reasons(self) -> None:
        a = self.a
        _, slots_out, _ = self.op("reason-slots", {"plan_id": self.plan, "project": self.project})
        slots = slots_out.get("slots", [])
        self.result["slots"] = len(slots)
        self.say(f"[4c] reason-slots: {len(slots)} slot(s)\n{slots_out.get('checklist', '')}")
        answers = self.answers(slots)
        filled = unstated = 0
        if answers:
            run_id = max(int(s["run_id"]) for s in slots)
            _, fill_out, _ = self.op("reason-fill", {"plan_id": self.plan, "project": self.project, "run_id": run_id,
                                                     "step_id": self.child, "reasons": answers})
            filled, unstated = int(fill_out.get("filled", 0)), int(fill_out.get("unstated", 0))
        self.result["filled"] = filled
        self.result["unstated"] = len(slots) - filled
        self.say(f"[4c] reason-fill: filled={filled} unstated={self.result['unstated']} (source {a.reasons})")
        for line in self.manual:
            self.say(line)                 # re-emitted at the tail: log_context keeps the last 50 lines
        summary = (f"review PASS via review_run.py: gates={self.result['gates'] or 'resumed'}, "
                   f"refreshed_sha={(self.result['refreshed_sha'] or '')[:10]}, slots={len(slots)}, filled={filled}"
                   + ("; " + "; ".join(self.manual) if self.manual else ""))
        self.op("complete-step", {"step_id": self.child, "summary": summary, "log_context": "\n".join(self.log)})
        rows = self.read("SELECT status FROM Plans WHERE plan_id = ?", self.plan)
        closed = rows[0][0] if rows else None
        self.result["closed"] = closed
        if closed != "COMPLETED":
            # FL-135: exit 0 is documented as "closed COMPLETED", and completing
            # this step is not the same thing — an unfinished sibling recovery
            # step, or a close-time refusal, leaves the plan short of it. The
            # code that reports the outcome must agree with the outcome.
            self.say(f"[4c] {self.child} is COMPLETED but the plan is {closed}: not closed COMPLETED — exit 1")
            self.finish(1)
        self.finish(0)

    def answers(self, slots: list[dict]) -> list[dict]:
        """Answers for reason-fill. `stub` / `unstated` are the deterministic
        modes of the tests; a file holds [{node_key|qualified_name, interpretation
        | utterance_id+span | unstated}] — the legacy `text` key is accepted here
        and sent as interpretation (asserted), never as stated."""
        mode = self.a.reasons
        if mode == "stub":
            return [{"node_key": s["node_key"], "interpretation": f"scenario: {'/'.join(s['event_types'])}"} for s in slots]
        if mode == "unstated":
            return [{"node_key": s["node_key"], "unstated": True} for s in slots]
        if mode == "ask":
            # the agent wants to see the closed checklist before answering: stop
            # here with REVIEW.1 IN_PROGRESS; the next run resumes at 4c.
            _die("--reasons ask: answer these slots in a JSON file of "
                 "[{qualified_name|node_key, interpretation | utterance_id+span | unstated}] and re-run with --reasons <file>\n"
                 + "\n".join(f"  {s['qualified_name']} ({'/'.join(s['event_types'])}) [{s['node_key']}]" for s in slots)
                 + f"\n{self.child} left IN_PROGRESS, no second refresh", 6)
        try:
            items = json.loads(Path(mode).read_text())
        except (OSError, ValueError) as e:
            _die(f"--reasons must be stub, unstated or a JSON file: {e}", 6)
        if not isinstance(items, list):
            _die("--reasons file must hold a list of {node_key|qualified_name, interpretation|utterance_id+span|unstated}", 6)
        by_key = {s["node_key"]: s for s in slots}
        by_qn = {s["qualified_name"]: s for s in slots}
        out, unknown = [], []
        for it in items:
            key = it.get("node_key") or by_qn.get(it.get("qualified_name", ""), {}).get("node_key")
            if key not in by_key:
                unknown.append(it.get("node_key") or it.get("qualified_name"))
                continue
            if it.get("utterance_id") is not None and it.get("span"):
                ans = {"node_key": key, "utterance_id": it["utterance_id"], "span": list(it["span"])}
            elif it.get("unstated") or str(it.get("interpretation", it.get("text", ""))).strip().lower() in ("", "unstated", "unknown"):
                ans = {"node_key": key, "unstated": True}
            else:
                ans = {"node_key": key, "interpretation": str(it.get("interpretation") or it.get("text")).strip()}
                if it.get("refs"):
                    ans["refs"] = list(it["refs"])
            if it.get("because"):                      # DP phase 2: the records this answer leaned on → influence(reason_because)
                ans["because"] = [int(x) for x in it["because"]]
            out.append(ans)
        if unknown:
            _die(f"unknown reason key(s) {unknown} — only this plan's open slots are accepted:\n"
                 + "\n".join(f"  {s['qualified_name']} [{s['node_key']}]" for s in slots)
                 + f"\n{self.child} left IN_PROGRESS; re-run with a corrected file to resume at 4c", 6)
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plan-id", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--registry", default=None)
    ap.add_argument("--reasons", default="unstated", help="stub | unstated | ask | <file.json>")
    ap.add_argument("--tests", default=None)
    ap.add_argument("--timeout-tests", type=float, default=DEFAULT_TIMEOUT_TESTS_S, metavar="SECONDS",
                    help=f"ceiling on the --tests command (default {DEFAULT_TIMEOUT_TESTS_S}s); a breach FAILS the step")
    ap.add_argument("--timeout-graph", type=float, default=DEFAULT_TIMEOUT_GRAPH_S, metavar="SECONDS",
                    help=f"ceiling on the init_project.sh refresh (default {DEFAULT_TIMEOUT_GRAPH_S}s); "
                         f"a breach FAILS the step (the write scripts are fixed at {TIMEOUT_WRITE_S}s)")
    ap.add_argument("--accept-signature", default=None, metavar="REASON")
    ap.add_argument("--accept-stale", default=None, metavar="REASON",
                    help="override the stale_references gate only (e.g. a definition moved and is re-exported; "
                         "the graph does not follow imports); the reason is logged")
    ap.add_argument("--as-recovery", default=None, metavar="STEP_ID",
                    help="run the review as this PENDING sub-step of a FAILED REVIEW.1 (FL-030)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="refresh even with uncommitted tracked changes (traced as a manual verdict)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    Driver(ap.parse_args()).run()


if __name__ == "__main__":
    main()
