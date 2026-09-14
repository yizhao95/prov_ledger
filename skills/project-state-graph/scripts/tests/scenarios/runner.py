"""Timeline-scenario runner (phase 4 Task 2) — see README.md.

A scenario = an ordered list of plans applied to a synthetic project inside a
fully isolated workspace. Every write goes through the public scripts:
publish-plan.sh -> run-step.sh -> review_run.py (which drives init_project.sh,
reason-slots.sh / reason-fill.sh, complete-step.sh / fail-step.sh). This module
never writes the orchestrator DB or the state graph itself; it only READS them
in collect().
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent            # tests/scenarios
PSG_SCRIPTS = HERE.parents[1]                     # project-state-graph/scripts
SKILLS = PSG_SCRIPTS.parents[1]                   # skills/
if str(PSG_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PSG_SCRIPTS))
from tests.corpus import mutate  # noqa: E402

INIT_PROJECT = PSG_SCRIPTS / "init_project.sh"
PUBLISH = SKILLS / "writing-plans" / "scripts" / "publish-plan.sh"
LEDGER_ADD = SKILLS / "writing-plans" / "scripts" / "ledger-add.sh"
RUN_STEP = SKILLS / "executing-plans" / "scripts" / "run-step.sh"
REVIEW_RUN = SKILLS / "update-project-state-graph" / "scripts" / "review_run.py"

VOLATILE = {"id", "created_at", "observed_at", "updated_at", "started_at", "completed_at", "commit_sha"}
TAG_RE = re.compile(r"\[[A-Z][A-Z0-9 _-]*\]")


@dataclass
class Workspace:
    root: Path
    repo: Path
    orch_db: Path
    registry: Path
    graphs: Path
    env: dict[str, str]
    project: str = "scn"
    task_shas: dict[str, str] = field(default_factory=dict)
    plan_ids: dict[str, str] = field(default_factory=dict)

    # ── plumbing ─────────────────────────────────────────────────────────────
    def sh(self, cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        p = subprocess.run(cmd, cwd=str(cwd or self.repo), env=self.env, capture_output=True, text=True, timeout=300)
        if check and p.returncode != 0:
            raise RuntimeError(f"{' '.join(map(str, cmd))} exited {p.returncode}\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
        return p

    def git(self, *args: str) -> str:
        return self.sh(["git", *args]).stdout.strip()

    def registry_entry(self) -> dict:
        reg = json.loads(self.registry.read_text())["projects"]
        return next(p for p in reg if p["name"] == self.project)

    def psg_db(self) -> str:
        return self.registry_entry()["db_path"]

    def read_orch(self, sql: str, *params):
        c = sqlite3.connect(str(self.orch_db))
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
        finally:
            c.close()

    def read_psg(self, sql: str, *params):
        c = sqlite3.connect(f"file:{self.psg_db()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
        finally:
            c.close()


# ── workspace ─────────────────────────────────────────────────────────────────

def _venv_root() -> Path:
    return Path(sys.executable).resolve().parents[1]


def make_workspace(tmp_path: Path, fixture_dir: Path, project: str = "scn") -> Workspace:
    """copy fixture -> git init/commit -> init_project.sh (baseline run 1).
    Everything the scripts touch lives under tmp_path; HOME included."""
    root = Path(tmp_path).resolve()
    repo = root / "repo"
    shutil.copytree(fixture_dir, repo, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    graphs = root / "graphs"
    graphs.mkdir()
    home = root / "home"
    home.mkdir()
    env = {**os.environ,
           "HOME": str(home),
           "ORCH_DB": str(root / "orchestrator.db"),
           "PSG_REGISTRY_ROOT": str(graphs),
           "PSG_REGISTRY_PATH": str(graphs / "projects.json"),
           "PROVLEDGER_VENV": str(_venv_root()),
           "GIT_AUTHOR_NAME": "scenario", "GIT_AUTHOR_EMAIL": "scenario@test",
           "GIT_COMMITTER_NAME": "scenario", "GIT_COMMITTER_EMAIL": "scenario@test",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    for k in ("PROVLEDGER_PLAN_ID", "PROVLEDGER_STEP_ID", "PROVLEDGER_TRIGGER"):
        env.pop(k, None)
    ws = Workspace(root=root, repo=repo, orch_db=root / "orchestrator.db", registry=graphs / "projects.json",
                   graphs=graphs, env=env, project=project)
    ws.git("init", "-q")
    ws.git("add", "-A")
    ws.git("commit", "-qm", "base")
    ws.sh(["bash", str(INIT_PROJECT), "--name", project, "--repo", str(repo)])
    ws.task_shas["base"] = ws.git("rev-parse", "HEAD")
    return ws


# ── changes ───────────────────────────────────────────────────────────────────

def apply_change(ws: Workspace, change: dict, task: str = "change") -> str:
    """{"files": {rel: content}} | {"generated": mutator} | {"replace": {rel: [[old, new], ...]}} |
    {"revert_to": task}; one commit; returns its sha."""
    if "files" in change:
        for rel, content in change["files"].items():
            p = ws.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    elif "generated" in change:
        name = change["generated"]
        if name not in mutate.GENERATED:
            raise ValueError(f"unknown generated mutator {name!r}; known: {sorted(mutate.GENERATED)}")
        mutate.GENERATED[name](ws.repo, ws.repo)
    elif "replace" in change:
        for rel, edits in change["replace"].items():
            f = ws.repo / rel
            text = f.read_text()
            for old, new in edits:
                n = text.count(old)
                if n != 1:
                    raise ValueError(f"replace in {rel}: {old!r} occurs {n} times, must be exactly once")
                text = text.replace(old, new)
            f.write_text(text)
    elif "revert_to" in change:
        target = change["revert_to"]
        if target not in ws.task_shas:
            raise ValueError(f"revert_to {target!r}: no such task; known: {sorted(ws.task_shas)}")
        ws.git("checkout", "-q", ws.task_shas[target], "--", ".")
    else:
        raise ValueError(f"change must be one of files / generated / replace / revert_to, got {sorted(change)}")
    ws.git("add", "-A")
    ws.git("commit", "-q", "--allow-empty", "-m", f"{task}: {'/'.join(sorted(change))}")
    sha = ws.git("rev-parse", "HEAD")
    ws.task_shas[task] = sha
    return sha


# ── setup items ───────────────────────────────────────────────────────────────

def node_key_of(ws: Workspace, qualified_name: str) -> str | None:
    rows = ws.read_psg("SELECT node_key FROM node_snapshot WHERE qualified_name = ? AND node_key <> '' "
                       "ORDER BY run_id DESC, id DESC LIMIT 1", qualified_name)
    return rows[0]["node_key"] if rows else None


LEDGER_CLI = SKILLS / "writing-plans" / "scripts" / "ledger_cli.py"


def setup(ws: Workspace, item: dict) -> None:
    if "ledger_import" in item:
        # declarative constraints: a provledger-extensions.json imported through
        # `ledger_cli.py import` (subjects resolve to node_keys via the graph)
        f = ws.root / "provledger-extensions.json"
        f.write_text(json.dumps({"version": 1, "constraints": item["ledger_import"]["constraints"]}, ensure_ascii=False))
        ws.sh([sys.executable, str(LEDGER_CLI), "import", str(f), "--project", ws.project])
        return
    if "ledger_constraint" in item:
        c = item["ledger_constraint"]
        subject = c.get("subject_key") or node_key_of(ws, c["subject"]) or c["subject"]
        cmd = ["bash", str(LEDGER_ADD), "add", "--project", ws.project, "--kind", "constraint",
               "--statement", c["statement"], "--rationale", c.get("rationale", ""),
               "--subjects", subject, "--why-visibility", c.get("visibility", "shared")]
        if c.get("why_ref"):
            cmd += ["--why-ref", c["why_ref"]]
        ws.sh(cmd)
    else:
        raise ValueError(f"unknown setup item {sorted(item)}")


# ── one task ──────────────────────────────────────────────────────────────────

def publish(ws: Workspace, task: dict) -> tuple[str, list[str]]:
    """publish-plan.sh from inside the repo (cwd attribution). Returns (plan_id, step_ids)."""
    name = task["name"]
    steps = task.get("steps") or [f"COMMAND: {task.get('goal', name)}"]
    plan = {"goal": task.get("goal", name), "prefix": task.get("prefix", name), "max_revisions": 5,
            "skills": [{"name": "executing-plans", "source": "iron-law"}],
            "steps": [{"description": s, "type": "COMMAND"} for s in steps]}
    for key in ("declared_targets", "expectations", "user_query", "project"):
        if key in task:
            plan[key] = task[key]
    inp = ws.root / f"plan-input-{name}.json"
    inp.write_text(json.dumps(plan, ensure_ascii=False))
    stdout = ws.sh(["bash", str(PUBLISH), str(inp)]).stdout
    out = json.loads(stdout[stdout.index("{"):])          # pretty-printed result JSON (no marker line)
    ws.plan_ids[name] = out["plan_id"]
    return out["plan_id"], out["step_ids"]


def run_steps(ws: Workspace, plan_id: str, step_ids: list[str]) -> None:
    for sid in step_ids:
        inp = ws.root / f"run-{sid}.json"
        inp.write_text(json.dumps({"step_id": sid, "type": "COMMAND", "command": "true",
                                   "summary": "scenario step (no-op command)"}))
        ws.sh(["bash", str(RUN_STEP), str(inp)])


def close(ws: Workspace, plan_id: str, task: dict) -> dict:
    """review_run.py --json when the plan is awaiting review; otherwise the plan's close as recorded."""
    child = ws.read_orch("SELECT status FROM Steps WHERE step_id = ?", f"{plan_id}-REVIEW.1")
    if not child:
        row = ws.read_orch("SELECT status, review_state, review_skip_reason FROM Plans WHERE plan_id = ?", plan_id)[0]
        return {"closed": row["status"], "review_state": row["review_state"], "review_skipped": row["review_skip_reason"],
                "slots": 0, "filled": 0, "unstated": 0, "reviewed": False}
    reasons = task.get("reasons", "unstated")
    if isinstance(reasons, dict):
        f = ws.root / f"reasons-{task['name']}.json"
        f.write_text(json.dumps([{"qualified_name": k, "text": v} for k, v in reasons.items()], ensure_ascii=False))
        reasons = str(f)
    cmd = [sys.executable, str(REVIEW_RUN), "--plan-id", plan_id, "--project", ws.project, "--json",
           "--reasons", reasons]
    if task.get("tests"):
        cmd += ["--tests", task["tests"]]
    if task.get("accept_signature"):
        cmd += ["--accept-signature", task["accept_signature"]]
    p = ws.sh(cmd, check=False)
    if p.returncode not in (0, 1):
        raise RuntimeError(f"review_run.py exited {p.returncode}\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
    out = json.loads([ln for ln in p.stdout.strip().splitlines() if ln.startswith("{")][-1])
    out["reviewed"] = True
    return out


def run_scenario(ws: Workspace, scenario: dict) -> dict:
    for item in scenario.get("setup", []):
        setup(ws, item)
    tasks_out = []
    for task in scenario["tasks"]:
        name = task["name"]
        sha = apply_change(ws, task["change"], task=name) if task.get("change") else ws.git("rev-parse", "HEAD")
        ws.task_shas.setdefault(name, sha)
        plan_id, step_ids = publish(ws, task)
        run_steps(ws, plan_id, step_ids)
        result = close(ws, plan_id, task)
        tasks_out.append({"name": name, "plan_id": plan_id, "sha": sha, "close": result})
    raw = collect(ws, [t["plan_id"] for t in tasks_out])
    names = {t["plan_id"]: t["name"] for t in tasks_out}
    return {"tasks": tasks_out, "raw": raw, "norm": normalize(raw, names)}


# ── collect (reads only) ──────────────────────────────────────────────────────

def collect(ws: Workspace, plan_ids: list[str]) -> dict:
    ph = ",".join("?" for _ in plan_ids) or "''"
    runs = ws.read_psg(f"SELECT id, plan_id, step_id, trigger, commit_sha FROM analysis_run "
                       f"WHERE plan_id IN ({ph}) OR plan_id IS NULL ORDER BY id", *plan_ids)
    run_ids = [r["id"] for r in runs]
    rph = ",".join("?" for _ in run_ids) or "''"
    events = ws.read_psg(f"SELECT id, run_id, seq, event_type, node_key, tier, payload_json, created_at "
                         f"FROM node_event WHERE run_id IN ({rph}) ORDER BY run_id, seq, id", *run_ids)
    for e in events:
        try:
            e["payload"] = json.loads(e.pop("payload_json") or "{}")
        except ValueError:
            e["payload"] = {}
    nodes = {}
    for r in ws.read_psg("SELECT node_key, qualified_name FROM node_snapshot WHERE node_key <> '' "
                         "ORDER BY run_id, id"):
        nodes[r["node_key"]] = r["qualified_name"]          # later rows win: latest name per key
    plans = ws.read_orch(f"SELECT plan_id, status, review_state, project, project_source, review_skip_reason, "
                         f"created_at, updated_at FROM Plans WHERE plan_id IN ({ph}) ORDER BY created_at, plan_id", *plan_ids)
    reasons = ws.read_orch(f"SELECT id, plan_id, node_key, kind, text, source, tier, run_id, created_at "
                           f"FROM node_reason WHERE plan_id IN ({ph}) ORDER BY id", *plan_ids)
    expectations = ws.read_orch(f"SELECT id, plan_id, target, target_kind, claim, channel, created_at "
                                f"FROM expectations WHERE plan_id IN ({ph}) ORDER BY id", *plan_ids)
    eph = ",".join("?" for _ in expectations) or "''"
    outcomes = ws.read_orch(f"SELECT id, expectation_id, kind, value_json, source, tier, reason, backfilled_by_plan, "
                            f"observed_at FROM outcomes WHERE expectation_id IN ({eph}) ORDER BY id",
                            *[e["id"] for e in expectations])
    for o in outcomes:
        try:
            o["value"] = json.loads(o.pop("value_json") or "{}")
        except ValueError:
            o["value"] = {}
    review_logs = []
    for s in ws.read_orch(f"SELECT plan_id, step_id, COALESCE(log_context, '') AS log FROM Steps "
                          f"WHERE plan_id IN ({ph}) AND (step_id LIKE '%-REVIEW' OR step_id LIKE '%-REVIEW.1') "
                          f"ORDER BY plan_id, step_id", *plan_ids):
        tags = sorted(set(TAG_RE.findall(s["log"])))
        review_logs.append({"plan_id": s["plan_id"], "step_id": s["step_id"], "tags": tags})
    return {"runs": runs, "node_events": events, "nodes": nodes, "plans": plans, "reasons": reasons,
            "expectations": expectations, "outcomes": outcomes, "review_logs": review_logs}


# ── normalize (pure) ──────────────────────────────────────────────────────────

def normalize(raw: dict, plan_names: dict[str, str]) -> dict:
    run_ord = {r["id"]: i + 1 for i, r in enumerate(sorted(raw["runs"], key=lambda r: r["id"]))}
    nodes = raw.get("nodes", {})
    ev_pos = {e["id"]: f"{run_ord.get(e['run_id'], '?')}.{e['seq']}" for e in raw["node_events"]}

    def plan(pid):
        return plan_names.get(pid, pid) if pid is not None else None

    def step(pid, sid):
        if sid is None:
            return None
        return sid[len(pid) + 1:] if pid and sid.startswith(pid + "-") else sid

    def node(key):
        return nodes.get(key, key) if key is not None else None

    def mapval(v, key=None):
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if k in VOLATILE:
                    continue
                if k in ("run_id", "prev_run_id"):
                    out[k[:-3]] = run_ord.get(x, x)
                elif k == "node_key":
                    out["node"] = node(x)
                elif k in ("plan_id", "backfilled_by_plan"):
                    out[k.replace("_plan", "").replace("plan_id", "plan")] = plan(x)
                elif k == "expectation_id":
                    continue
                else:
                    out[k] = mapval(x, k)
            return out
        if isinstance(v, list):
            if key == "evidence":
                return [ev_pos.get(x, str(x)) for x in v]
            if key in ("plans", "changed_by"):
                return [plan(x) for x in v]
            return [mapval(x) for x in v]
        if isinstance(v, str):
            if v in nodes:
                return nodes[v]
            if v in plan_names:
                return plan_names[v]
        return v

    runs = [{"run": run_ord[r["id"]], "plan": plan(r["plan_id"]), "step": step(r["plan_id"], r["step_id"]),
             "trigger": r["trigger"]} for r in sorted(raw["runs"], key=lambda r: r["id"])]
    run_plan = {r["id"]: plan(r["plan_id"]) for r in raw["runs"]}
    events: list[dict] = []
    for e in sorted(raw["node_events"], key=lambda e: (run_ord.get(e["run_id"], 0), e["seq"], e["id"])):
        ev = {"plan": run_plan.get(e["run_id"]), "run": run_ord.get(e["run_id"]), "type": e["event_type"],
              "node": node(e["node_key"]), "tier": e["tier"]}
        ev.update(mapval(e.get("payload") or {}))
        events.append(ev)
    for r in sorted(raw["reasons"], key=lambda r: r["id"]):
        events.append({"plan": plan(r["plan_id"]), "run": run_ord.get(r["run_id"], r["run_id"]), "type": r["kind"],
                       "node": node(r["node_key"]), "text_is": "stated" if r["text"] else "unstated",
                       "text": r["text"], "source": r["source"], "tier": r["tier"]})
    exp_by_id = {e["id"]: e for e in raw["expectations"]}
    for e in sorted(raw["expectations"], key=lambda e: e["id"]):
        events.append({"plan": plan(e["plan_id"]), "type": "expectation", "target": e["target"],
                       "target_kind": e["target_kind"], "claim": e["claim"], "channel": e["channel"]})
    for o in sorted(raw["outcomes"], key=lambda o: o["id"]):
        e = exp_by_id.get(o["expectation_id"], {})
        value = mapval(o.get("value") or {})
        events.append({"plan": plan(e.get("plan_id")), "type": "outcome", "target": e.get("target"),
                       "kind": o["kind"], "signal": value.get("signal") if isinstance(value, dict) else None,
                       "plans": (value.get("plans") or value.get("changed_by")) if isinstance(value, dict) else None,
                       "value": value, "source": o["source"], "tier": o["tier"], "reason": o["reason"],
                       "backfilled_by": plan(o["backfilled_by_plan"])})
    order = {pid: i for i, pid in enumerate(plan_names)}
    for p in sorted(raw["plans"], key=lambda p: order.get(p["plan_id"], 999)):
        events.append({"plan": plan(p["plan_id"]), "type": "plan", "closed": p["status"],
                       "review_state": p["review_state"], "project": p["project"],
                       "project_source": p["project_source"], "review_skipped": p["review_skip_reason"]})
    for s in raw.get("review_logs", []):
        for tag in s["tags"]:
            events.append({"plan": plan(s["plan_id"]), "type": "review_log",
                           "step": step(s["plan_id"], s["step_id"]), "tag": tag})
    return {"runs": runs, "events": events}


# ── expectations ──────────────────────────────────────────────────────────────

def _matches(expected: dict, event: dict) -> bool:
    for k, want in expected.items():
        if k.endswith("_contains"):
            have = event.get(k[:-9])
            if not (isinstance(have, str) and want in have):
                return False
        elif k.endswith("_startswith"):
            have = event.get(k[:-11])
            if not (isinstance(have, str) and have.startswith(want)):
                return False
        elif isinstance(want, list):
            have = event.get(k)
            if not (isinstance(have, list) and all(x in have for x in want)):
                return False
        elif event.get(k, _MISSING) != want:
            return False
    return True


_MISSING = object()


def check_expect(norm: dict, expect: dict) -> list[str]:
    """expect.events: every entry must match >= 1 event (subset match);
    must_not (mandatory): no entry may match any event; {anywhere: s} = substring of the JSON."""
    if "must_not" not in expect:
        raise ValueError("every scenario must declare must_not (an empty list is not a scenario)")
    events = norm["events"]
    text = json.dumps(norm, ensure_ascii=False, sort_keys=True)
    errors = []
    for entry in expect.get("events", []):
        if not any(_matches(entry, ev) for ev in events):
            errors.append(f"missing: {json.dumps(entry, ensure_ascii=False)}")
    for entry in expect["must_not"]:
        if "anywhere" in entry:
            if entry["anywhere"] in text:
                errors.append(f"forbidden: text {entry['anywhere']!r} appears in the normalised output")
            continue
        hit = next((ev for ev in events if _matches(entry, ev)), None)
        if hit is not None:
            errors.append(f"forbidden: {json.dumps(entry, ensure_ascii=False)} matched {json.dumps(hit, ensure_ascii=False)}")
    return errors
