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
        [--accept-signature "<reason>"]         # may override ONLY the signature gate; reason is logged
        [--dry-run]                             # steps 0–3 only, nothing written
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

Exit codes: 0 closed COMPLETED · 1 closed FAILED (or a failing --dry-run verdict)
· 5 a write script failed · 6 bad input (unknown project / reasons key / file).
"""
from __future__ import annotations

import argparse
import json
import os
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
        self.manual: str | None = None
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
        p = subprocess.run(["bash", str(EXEC / f"{script}.sh"), str(f)], env=self.env,
                           capture_output=True, text=True)
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

    def finish(self, exit_code: int) -> None:
        if self.a.json:
            print(json.dumps(self.result, ensure_ascii=False))
        sys.exit(exit_code)

    # ── closes ───────────────────────────────────────────────────────────────
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
            self.op("start-step", {"step_id": self.child, "type": "SUB_AGENT",
                                   "agent_input": f"review_run.py --plan-id {self.plan} --project {self.project}"
                                                  f" --reasons {a.reasons} --tests {a.tests!r}",
                                   "log_context": lock})
        if not db_path or not os.path.exists(db_path):
            reason = f"no deep graph for {self.project!r} (db_path={db_path}) — run project-state-graph first"
            if a.dry_run:
                self.say(reason)
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
        if not ok and a.accept_signature:
            failing = [g for g, v in verdict["gates"].items() if not v]
            if failing == ["signature"]:
                ok = True
                self.manual = f"[MANUAL VERDICT] signature gate overridden: {a.accept_signature}"
                self.say(self.manual)
            else:
                self.say(f"[MANUAL VERDICT] --accept-signature ignored: failing gates {failing} are not just signature")
        self.result["verdict"] = ok
        if a.dry_run:
            self.say(f"[dry-run] verdict {'PASS' if ok else 'FAIL'}; nothing written")
            self.finish(0 if ok else 1)
        if not ok:
            gaps = {g: v for g, v in verdict["gaps"].items() if not verdict["gates"].get(g, True)}
            self.fail(f"gates failed {[g for g in gaps]}: {json.dumps(gaps, ensure_ascii=False)[:1500]}")

        # 4b. refresh with attribution, re-test, selfcheck
        env = dict(self.env, PROVLEDGER_PLAN_ID=self.plan, PROVLEDGER_STEP_ID=self.child, PROVLEDGER_TRIGGER="review")
        p = subprocess.run(["bash", str(INIT_PROJECT), "--name", self.project, "--repo", repo],
                           env=env, capture_output=True, text=True)
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
            t = subprocess.run(a.tests, shell=True, cwd=repo, capture_output=True, text=True)
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
        if self.manual:
            self.say(self.manual)          # re-emitted at the tail: log_context keeps the last 50 lines
        summary = (f"review PASS via review_run.py: gates={self.result['gates'] or 'resumed'}, "
                   f"refreshed_sha={(self.result['refreshed_sha'] or '')[:10]}, slots={len(slots)}, filled={filled}"
                   + (f"; {self.manual}" if self.manual else ""))
        self.op("complete-step", {"step_id": self.child, "summary": summary, "log_context": "\n".join(self.log)})
        rows = self.read("SELECT status FROM Plans WHERE plan_id = ?", self.plan)
        self.result["closed"] = rows[0][0] if rows else "COMPLETED"
        self.finish(0)

    def answers(self, slots: list[dict]) -> list[dict]:
        mode = self.a.reasons
        if mode == "stub":
            return [{"node_key": s["node_key"], "text": f"scenario: {'/'.join(s['event_types'])}"} for s in slots]
        if mode == "unstated":
            return [{"node_key": s["node_key"], "text": "unstated"} for s in slots]
        if mode == "ask":
            # the agent wants to see the closed checklist before answering: stop
            # here with REVIEW.1 IN_PROGRESS; the next run resumes at 4c.
            _die("--reasons ask: answer these slots in a JSON file of "
                 "[{qualified_name|node_key, text}] and re-run with --reasons <file>\n"
                 + "\n".join(f"  {s['qualified_name']} ({'/'.join(s['event_types'])}) [{s['node_key']}]" for s in slots)
                 + f"\n{self.child} left IN_PROGRESS, no second refresh", 6)
        try:
            items = json.loads(Path(mode).read_text())
        except (OSError, ValueError) as e:
            _die(f"--reasons must be stub, unstated or a JSON file: {e}", 6)
        if not isinstance(items, list):
            _die("--reasons file must hold a list of {node_key|qualified_name, text}", 6)
        by_key = {s["node_key"]: s for s in slots}
        by_qn = {s["qualified_name"]: s for s in slots}
        out, unknown = [], []
        for it in items:
            key = it.get("node_key") or by_qn.get(it.get("qualified_name", ""), {}).get("node_key")
            if key not in by_key:
                unknown.append(it.get("node_key") or it.get("qualified_name"))
                continue
            out.append({"node_key": key, "text": it.get("text", "unstated")})
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
    ap.add_argument("--accept-signature", default=None, metavar="REASON")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    Driver(ap.parse_args()).run()


if __name__ == "__main__":
    main()
