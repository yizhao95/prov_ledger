#!/usr/bin/env python3
"""stage0_plugin — a plugin user: the install, a cold first session, a first task.

The plugin path was never exercised before this stage existed. Stage 1 installs
by hand, and the /ledger and /receipts checks played the session themselves.
The first time a sandbox install was tried, a plugin user's `/ledger` turned out
to be broken — `provledger` was not on the session's PATH (FL-195) — with every
suite green. So this stage does what a user does, in a HOME and a Claude
configuration of its own:

  S0a  install the plugin from a clone of the tree under test (`claude plugin
       marketplace add` + `install`), then one cold session in a fresh repo:
       the SessionStart bootstrap builds the venv, `provledger` resolves to the
       plugin's own bin/, and the hooks create the ledger and record the
       session's words, its tool calls (a failed one included) and its run.
  S1   the first task. The dashboard, started before there is any ledger, says
       so and picks the ledger up when the first plan lands. Then the agent is
       given a small task and plans and runs it itself through writing-plans /
       executing-plans. What is checked is the outcome — a plan published, every
       step terminal, a COMMAND step completed (only run-step.sh can complete
       one), the plan closed, the code really changed, the plan and the first
       session's words on the dashboard — never how the agent split the work.

Stage 2 reuses this install for the real `/ledger` and `/receipts` sessions
(`ensure_installed`).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plugin_session as PS                                         # noqa: E402
from e2elib import FAIL, OK, LOGS, ROOT, Tally, banner, info, step, write_verdict  # noqa: E402

REAL_HOME = Path(os.environ.get("E2E_REAL_HOME") or Path.home())
REPO = Path(os.environ.get("E2E_REPO") or Path(__file__).resolve().parents[2])
CLONE_FROM = os.environ.get("E2E_CLONE_FROM") or str(REPO)
MODEL = (os.environ.get("E2E_MODEL") or "").strip() or None

HOME = ROOT / "plugin-home"
CONFIG = ROOT / "plugin-config"
SOURCE = ROOT / "plugin-src"
STATE = ROOT / "plugin.json"
LEDGER = HOME / "skill-workspace" / "orchestrator.db"
VENV = HOME / "skill-workspace" / ".venv"


def env() -> dict:
    e = PS.stranger_env(HOME, CONFIG)
    # Logs a stranger would find in /tmp; kept in the sandbox so two runs, or a
    # run and the developer's own dashboard, never share one.
    e["PROVLEDGER_BOOTSTRAP_LOG"] = str(LOGS / "plugin-bootstrap.log")
    e["PROVLEDGER_DASH_LOG"] = str(LOGS / "plugin-dashboard.log")
    return e


def remember_port(port: int) -> None:
    """release-e2e.sh stops whatever is still listening on these at exit."""
    with (ROOT / "ports.txt").open("a", encoding="utf-8") as f:
        f.write(f"{port}\n")


def version_of(src: Path) -> str:
    text = (src / "orchestrator-backend" / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "?"


def q(sql: str, args: tuple = ()) -> list | None:
    """Read the plugin user's ledger; None when it cannot be read."""
    try:
        c = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True)
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return None


def wait_for(cond, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(3)
    return bool(cond())


# ── the install ──────────────────────────────────────────────────────────────
def ensure_installed(t: Tally) -> dict | None:
    """The plugin installed into this sandbox's own Claude configuration, once.
    Returns what a later session needs, or None when it could not be installed."""
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    step("install the plugin as INSTALL.md says: marketplace add, then install")
    if not SOURCE.exists():
        p = subprocess.run(["git", "clone", "-q", "--no-hardlinks", CLONE_FROM, str(SOURCE)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            t.record(FAIL, "clone the tree under test as the plugin's source", p.stderr[-600:])
            return None
    HOME.mkdir(parents=True, exist_ok=True)
    PS.seed_config(CONFIG, REAL_HOME)
    rc, said = PS.install_plugin(SOURCE, env())
    (LOGS / "plugin-install.log").write_text(said, encoding="utf-8")
    if rc != 0:
        t.record(FAIL, "claude plugin marketplace add + install", said[-900:])
        return None
    listing = PS.plugin_list(env())
    ver = version_of(SOURCE)
    enabled = PS.PLUGIN in listing and ver in listing and "enabled" in listing
    t.record(OK if enabled else FAIL, f"the plugin is installed and enabled at {ver}",
             "" if enabled else listing[-600:])
    if not enabled:
        return None
    state = {"home": str(HOME), "config": str(CONFIG), "source": str(SOURCE), "version": ver}
    STATE.write_text(json.dumps(state), encoding="utf-8")
    return state


def first_repo() -> Path:
    proj = HOME / "first-project"
    if not (proj / ".git").exists():
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "calc.py").write_text("def sub(a, b):\n    return a - b\n", encoding="utf-8")
        for argv in (["git", "init", "-q"], ["git", "add", "calc.py"],
                     ["git", "-c", "user.email=e2e@example.invalid", "-c", "user.name=e2e",
                      "commit", "-qm", "init"]):
            subprocess.run(argv, cwd=proj, check=True, capture_output=True)
    return proj


# ── S0a · the cold session ───────────────────────────────────────────────────
COLD_PROMPT = ("Run exactly these three shell commands, one at a time, and report each output "
               "verbatim: (1) command -v provledger  (2) provledger --help  "
               "(3) command -v definitely-not-a-tool")


def cold_session(t: Tally) -> PS.Transcript | None:
    step("S0a · a cold session in a fresh repo: bootstrap, PATH, hooks")
    s = PS.run_session(COLD_PROMPT, cwd=first_repo(), env=env(),
                       allowed_tools=("Bash(command -v:*)", "Bash(provledger:*)"),
                       max_turns=8, timeout_s=300, model=MODEL, log=LOGS / "s0a-session.jsonl")
    if not s.ok:
        t.record(PS.session_verdict(s), "the cold session answers",
                 s.blocked or f"rc={s.rc} · {s.stderr[-500:]} · stream {s.raw_path}")
        return None
    t.record(OK, "the cold session answers", f"{s.num_turns} turns in {s.elapsed_s} s · session {s.session_id}")
    root = s.plugins.get("provledger")
    t.record(OK if root else FAIL, "the session loaded the plugin", root or f"plugins: {sorted(s.plugins)}")

    want = str(Path(root) / "bin" / "provledger") if root else "<plugin>/bin/provledger"
    where = next((c for c in s.calls if c.input.get("command", "").strip() == "command -v provledger"), None)
    got = where.output.strip() if where else ""
    t.record(OK if got == want else FAIL, "`provledger` is on the session's PATH, from the plugin's bin/ (FL-195)",
             f"command -v provledger → {got or '(nothing)'}" + ("" if got == want else f"; expected {want}"))

    helps = next((c for c in s.calls if c.input.get("command", "").strip().startswith("provledger --help")), None)
    if helps and "usage: provledger" in helps.output:
        t.record(OK, "`provledger --help` answers in the first session")
    elif helps and "bootstrap" in helps.output:
        t.record(OK, "`provledger --help` in the first session says the venv is still being built",
                 "the SessionStart bootstrap runs async and the wrapper names what builds the venv: "
                 + helps.output.strip()[:200])
    else:
        t.record(FAIL, "`provledger --help` answers in the first session",
                 helps.output[:400] if helps else "the session never ran it")

    ready = wait_for(lambda: (VENV / ".provledger-reqs.sha256").exists()
                     and (VENV / "bin" / "provledger").exists(), 300)
    t.record(OK if ready else FAIL, "the SessionStart bootstrap built the plugin venv",
             str(VENV) if ready else f"no marker or console script after 300 s; see {LOGS / 'plugin-bootstrap.log'}")
    hooks_check(t, s)
    return s


def hooks_check(t: Tally, s: PS.Transcript) -> None:
    if not LEDGER.exists():
        t.record(FAIL, "the hooks created the ledger", f"{LEDGER} does not exist")
        return
    t.record(OK, "the hooks created the ledger", str(LEDGER))
    words = q("SELECT text FROM utterance WHERE session_id = ?", (s.session_id,)) or []
    t.record(OK if any("definitely-not-a-tool" in w[0] for w in words) else FAIL,
             "the session's words are recorded", f"{len(words)} utterance row(s) for {s.session_id}")
    calls = q("SELECT command_head, failed FROM tool_call_log WHERE session_id = ?", (s.session_id,)) or []
    failed = [c for c in calls if c[1] == 1]
    fine = [c for c in calls if c[1] == 0]
    t.record(OK if failed and fine else FAIL, "every tool call is logged, the failed one marked (FL-208)",
             f"{len(fine)} ok, {len(failed)} failed: {calls[:6]}")
    run = q("SELECT count(*) FROM session_run WHERE session_id = ?", (s.session_id,)) or [(0,)]
    t.record(OK if run[0][0] else FAIL, "the Stop hook recorded the session's run", f"{run[0][0]} session_run row(s)")
    errors = HOME / "skill-workspace" / "hook-errors.log"
    said = errors.read_text(encoding="utf-8").strip() if errors.exists() else ""
    t.record(FAIL if said else OK, "no hook logged an error", said[-600:])


# ── S1 · the first task ──────────────────────────────────────────────────────
def dashboard_before_a_ledger(t: Tally, root: Path) -> None:
    step("S1 · the dashboard before there is a ledger")
    port = PS.free_port()
    remember_port(port)
    cold = ROOT / "cold-ledger" / "orchestrator.db"             # nothing there yet
    e = {**env(), "ORCH_DB": str(cold), "PROVLEDGER_DASH_PORT": str(port)}
    base = f"http://127.0.0.1:{port}"
    try:
        p = subprocess.run(["bash", str(root / "skills" / "writing-plans" / "scripts" / "ensure-dashboard.sh")],
                           env=e, capture_output=True, text=True, timeout=90)
        said = (p.stdout + p.stderr).strip()
        t.record(OK if p.returncode == 0 and "no ledger yet" in said else FAIL,
                 "ensure-dashboard.sh starts it and says there is no ledger yet", said[-400:])
        code, body = PS.http_get(f"{base}/")
        t.record(OK if code == 200 and "publish-plan.sh" in body else FAIL,
                 "the empty dashboard points at publish-plan.sh", f"GET / → {code}")
        plan = ROOT / "cold-plan.json"
        plan.write_text(json.dumps({
            "goal": "release check: the first plan on a machine with no ledger", "prefix": "cold",
            "project": "none",
            "steps": [{"type": "THINKING", "description": "THINKING: this plan exists to create the ledger"}]}),
            encoding="utf-8")
        p2 = subprocess.run(["bash", str(root / "skills" / "writing-plans" / "scripts" / "publish-plan.sh"), str(plan)],
                            env=e, capture_output=True, text=True, timeout=180, cwd=str(ROOT))
        try:
            pid = json.loads(p2.stdout)["plan_id"]
        except (ValueError, KeyError, TypeError):
            t.record(FAIL, "publish-plan.sh creates the ledger with the first plan", (p2.stderr or p2.stdout)[-500:])
            return
        t.record(OK if cold.exists() else FAIL, "publish-plan.sh creates the ledger with the first plan", pid)
        healthy = wait_for(lambda: PS.http_get(f"{base}/api/health")[0] == 200, 30)
        code, body = PS.http_get(f"{base}/")
        t.record(OK if healthy and pid in body else FAIL, "the running dashboard picks the new ledger up",
                 f"/api/health {'200' if healthy else 'not 200'} · {pid} {'on' if pid in body else 'not on'} /")
    finally:
        PS.stop_port(port)


TASK = ("Add a function add(a, b) that returns a + b to calc.py, and a script check_calc.py that "
        "asserts add(2, 3) == 5 and sub(5, 3) == 2; run it with python3. Plan this with the "
        "writing-plans skill first, then carry the plan out with the executing-plans skill.")
TASK_TOOLS = ("Skill", "Bash", "Read", "Write", "Edit", "Glob", "Grep")


def root_check(q, plan_id: str) -> tuple[bool, str]:
    """Task-level redesign, step 2: every published plan has a plan_root row —
    the agent's own judgement (new / continues), or unknown when it named none.
    The kind is reported, not judged: naming a root is optional."""
    rows = q("SELECT kind, root_plan_id FROM plan_root WHERE plan_id = ? ORDER BY id DESC LIMIT 1", (plan_id,)) or []
    if not rows:
        return False, f"the agent's plan records a root: no plan_root row for {plan_id}"
    kind = rows[0][0]
    return True, f"the agent's plan records a root: {kind}" + (" (none named)" if kind == "unknown" else "")


def first_task(t: Tally, root: Path, cold: PS.Transcript | None) -> None:
    step("S1 · the first task: the agent plans it and runs it itself")
    port = PS.free_port()
    remember_port(port)
    e = {**env(), "PROVLEDGER_DASH_PORT": str(port)}
    base = f"http://127.0.0.1:{port}"
    proj = first_repo()
    before = {r[0] for r in (q("SELECT plan_id FROM Plans") or [])}
    try:
        s = PS.run_session(TASK, cwd=proj, env=e, allowed_tools=TASK_TOOLS, max_turns=80,
                           timeout_s=1200, model=MODEL, log=LOGS / "s1-session.jsonl")
        t.record(PS.session_verdict(s), "the first-task session finishes",
                 s.blocked or f"{s.num_turns} turns in {s.elapsed_s} s" + ("" if s.ok else f" · rc={s.rc} {s.stderr[-300:]}"))
        if s.blocked:
            return
        new = [r for r in (q("SELECT plan_id, status FROM Plans ORDER BY created_at") or []) if r[0] not in before]
        if not new:
            t.record(FAIL, "the agent published a plan through writing-plans",
                     f"no new plan in {LEDGER}; its commands: {s.bash_commands()[:8]}")
            return
        pid, status = new[-1]
        t.record(OK, "the agent published a plan through writing-plans", f"{pid} ({len(new)} new)")
        steps = q("SELECT step_id, step_type, status, is_review FROM Steps WHERE plan_id = ?", (pid,)) or []
        regular = [x for x in steps if not x[3]]
        still_open = [x[0] for x in regular if x[2] not in ("COMPLETED", "FAILED")]
        t.record(OK if regular and not still_open else FAIL, "every step reached a terminal state",
                 f"{len(regular)} step(s); still open: {still_open[:4]}")
        commands = [x[0] for x in regular if x[1] == "COMMAND" and x[2] == "COMPLETED"]
        t.record(OK if commands else FAIL, "a COMMAND step completed — only run-step.sh can complete one",
                 f"{commands[:3]}" if commands else f"step types: {sorted({x[1] for x in regular})}")
        t.record(OK if status == "COMPLETED" else FAIL, "the plan closed by itself", f"status {status}")
        ok, said = root_check(q, pid)
        t.record(OK if ok else FAIL, said)
        chk = subprocess.run(["python3", "-c", "import calc; assert calc.add(2, 3) == 5"], cwd=proj,
                             capture_output=True, text=True)
        t.record(OK if chk.returncode == 0 else FAIL, "the code really changed: calc.add(2, 3) == 5",
                 chk.stderr.strip()[-300:])

        up = PS.http_get(f"{base}/api/health")[0] == 200
        t.record(OK if up else FAIL, "writing-plans brought the dashboard up (its step 2)", base)
        if not up:     # bring it up ourselves, so the page checks below are about the pages
            subprocess.run(["bash", str(root / "orchestrator-webapp" / "launch_dashboard.sh")], env=e,
                           capture_output=True, text=True, timeout=90)
        code, body = PS.http_get(f"{base}/plan/{pid}")
        t.record(OK if code == 200 and pid in body else FAIL, "the plan is on the dashboard", f"GET /plan/{pid} → {code}")
        if cold:
            code, body = PS.http_get(f"{base}/session/{cold.session_id}")
            t.record(OK if code == 200 and "definitely-not-a-tool" in body else FAIL,
                     "the first session's words are on its session page", f"GET /session/{cold.session_id} → {code}")
    finally:
        PS.stop_port(port)


def main() -> int:
    banner("STAGE 0 · a plugin user: the install, a cold first session, a first task")
    t = Tally()
    info(f"plugin HOME  {HOME}")
    info(f"claude conf  {CONFIG}   (the login only; the plugin under test is the one plugin)")
    info(f"source       {CLONE_FROM}   (committed HEAD, cloned)")
    if ensure_installed(t):
        cold = cold_session(t)
        root = Path(cold.plugins["provledger"]) if cold and cold.plugins.get("provledger") else SOURCE
        dashboard_before_a_ledger(t, root)
        first_task(t, root, cold)
    write_verdict("0", t.verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
