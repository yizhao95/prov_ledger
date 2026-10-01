#!/usr/bin/env python3
"""stage2_surfaces — a dummy project, then the three ways in.

Build a small project whose whole history we wrote (`dummy_project.py`), then
exercise every surface a person actually reaches it through:

  · `/ledger`   — the user's own question about the project's history
  · `/receipts` — helping the user answer a colleague who challenged something
  · the dashboard — clicked by a real Chromium, because 267 server-side
                    rendering tests could not see a week-long click outage

and one property that is not a surface at all but belongs here because it can
only be checked by running the flows: **the answering happens in the session
that asked.** `/ledger` and `/receipts` are instructions for the model already
in the room; neither may fork a second `claude` when nobody asked for one
(accountability design §8.8). That is proved below by shadowing `claude` on
PATH with a stub that fails loudly, running the documented flows, and checking
the stub was never touched — with a control run that proves the stub does fire
when `--runner claude` asks for it.

The answers themselves are written by a stand-in for the session model (see
session_model.py) and marked in stage 3.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dummy_project as DP                                          # noqa: E402
import session_model as SM                                          # noqa: E402
from e2elib import (BLOCKED, FAIL, OK, LOGS, ROOT, Tally,  # noqa: E402
                    UNDER_TEST, banner, info, step, write_verdict)

PYTHON = os.environ.get("E2E_PYTHON") or sys.executable
NONCE = os.environ.get("E2E_NONCE", "dev")
MODEL = (os.environ.get("E2E_MODEL") or "").strip() or None


# ── 2b · nothing forks a second model unless asked ───────────────────────────
def shadow_dir() -> Path:
    """A directory holding a `claude` that refuses to run and says it was run.

    Shadowing PATH rather than watching for child processes, for two reasons: a
    child that starts and exits between two polls is simply missed, and a stub
    leaves durable evidence — a file — instead of a sighting. The one thing it
    does not catch is a call by absolute path, and the code in question builds
    its command as `["claude", ...]`, so PATH is the only route it can take.
    """
    d = ROOT / "shadow-bin"
    d.mkdir(parents=True, exist_ok=True)
    witness = d / "invoked.txt"
    stub = d / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "# A release-check stub. If this runs, something forked a model.\n"
        f'printf "%s\\n" "$*" >> "{witness}"\n'
        'echo "release-e2e: a model was forked when none was asked for" >&2\n'
        "exit 97\n", encoding="utf-8")
    stub.chmod(0o755)
    if witness.exists():
        witness.unlink()
    return d


def runner_default_check(t: Tally, cli, project: str, question: str) -> None:
    step("the documented flows fork no model of their own (accountability design §8.8)")
    d = shadow_dir()
    witness = d / "invoked.txt"
    # PROVLEDGER_ASK_RUNNER unset is half the property under test, so it is
    # removed from the child's environment rather than merely not added.
    env = {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}", "PROVLEDGER_ASK_RUNNER": None}

    # the /ledger flow, exactly as skills/ledger/SKILL.md runs it
    rc_a, out_a = cli(["ask", question, "--project", project, "--json", "--no-model"], env=env)
    # and the same with nothing said about the model at all: the default is the
    # property under test, so --no-model must NOT be passed here
    rc_b, out_b = cli(["ask", question, "--project", project, "--json"], env=env)
    rc_c, out_c = cli(["receipts", "someone asked where this number came from",
                       "--project", project, "--json"], env=env)
    forked = witness.read_text(encoding="utf-8").splitlines() if witness.exists() else []
    if forked:
        t.finding(
            "a documented flow forked a second `claude` with no session and no tools",
            f"{len(forked)} invocation(s) while running `ask --no-model`, a bare `ask` and "
            f"`receipts` with $PROVLEDGER_ASK_RUNNER unset. The default must be the model "
            f"already in the room (accountability design §8.8, point 4: `_ask_runner_default()` "
            f"returns `none`). First call: {forked[0][:160]!r}")
    else:
        t.record(OK, "no model was forked by `ask --no-model`, a bare `ask`, or `receipts`",
                 f"exit codes {rc_a}/{rc_b}/{rc_c} with `claude` shadowed by a stub that exits 97")

    # the control: the stub must be reachable, or the check above proves nothing
    if witness.exists():
        witness.unlink()
    cli(["ask", question, "--project", project, "--json", "--runner", "claude",
         "--timeout", "20"], env=env)
    if witness.exists():
        t.record(OK, "`--runner claude` still reaches a model",
                 "the shadow stub fired, so the check above is not vacuous and the capability "
                 "is not gone — only the default")
    else:
        t.record(FAIL, "`--runner claude` still reaches a model",
                 "the shadow stub was never invoked even when asked for explicitly, so the "
                 "no-fork result above cannot be trusted: the detector may simply not work")


# ── the dashboard ────────────────────────────────────────────────────────────
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def browser_python() -> tuple[str | None, str]:
    """An interpreter that can `import playwright`. Playwright is deliberately
    not in requirements.txt — it would push a 150 MB browser onto every plugin
    user — so the check looks for one and says plainly when there is none."""
    real = os.environ.get("E2E_REAL_HOME") or str(Path.home())
    seen = []
    for cand in (PYTHON,
                 str(Path.home() / "skill-workspace" / ".venv" / "bin" / "python"),
                 f"{real}/skill-workspace/.venv/bin/python",
                 shutil.which("python3") or ""):
        if not cand or cand in seen or not Path(cand).exists():
            continue
        seen.append(cand)
        p = subprocess.run([cand, "-c", "import playwright, sys; print(sys.executable)"],
                           capture_output=True, text=True)
        if p.returncode == 0:
            return cand, f"playwright from {cand}"
    return None, ("no interpreter with playwright installed (tried: " + ", ".join(seen) + ")")


def dashboard_deps(t: Tally) -> None:
    """The dashboard's graph and ask pages need `provledger` importable, and
    INSTALL.md §4's dependency line does not install it. Without it the browser
    checks below fail for a reason that has nothing to do with the browser —
    which is worth saying out loud, and then getting past, so the click checks
    test clicks."""
    probe = subprocess.run([PYTHON, "-c", "import provledger, httpx"],
                           capture_output=True, text=True)
    if probe.returncode == 0:
        return
    missing = " ".join(w for w in ("provledger", "httpx")
                       if f"named '{w}'" in probe.stderr or f"named {w}" in probe.stderr)
    t.finding(f"the dashboard needs {missing or 'provledger'} and INSTALL.md §4's dependency "
              f"line does not install it",
              "§6 says to launch the dashboard right after §4, and a dashboard launched that "
              "way renders the graph and the ask page as 'unavailable' — 200 pages that say "
              "they have nothing, so no status-code check notices. Installed "
              "`-r requirements.txt` (which does list it, as `-e ./orchestrator-backend`) so "
              "the browser checks below are about the browser.")
    for argv in ([PYTHON, "-m", "pip", "install", "--quiet", "-r", str(UNDER_TEST / "requirements.txt")],
                 ["uv", "pip", "install", "--quiet", "-r", str(UNDER_TEST / "requirements.txt")]):
        env = dict(os.environ, VIRTUAL_ENV=str(Path(PYTHON).parent.parent))
        if subprocess.run(argv, cwd=str(UNDER_TEST), env=env,
                          capture_output=True, text=True).returncode == 0:
            return


def dashboard_check(t: Tally, project: str, db: str) -> None:
    step("the dashboard, driven by a real browser")
    dashboard_deps(t)
    py, why = browser_python()
    if not py:
        t.record(BLOCKED, "drive the dashboard with a real browser",
                 f"{why}. Install it where the check can see it:\n"
                 f"  {PYTHON} -m pip install playwright && {PYTHON} -m playwright install chromium")
        return
    info(why)

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log = LOGS / "dashboard.log"
    # ORCH_DB is read at import time in app/queries.py, so it has to be in the
    # environment before uvicorn boots — not set per request.
    env = dict(os.environ, ORCH_DB=db)
    with log.open("w", encoding="utf-8") as lf:
        server = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(UNDER_TEST / "orchestrator-webapp"), env=env, stdout=lf, stderr=lf)
    try:
        healthy = False
        for _ in range(60):
            if server.poll() is not None:
                break
            try:
                import urllib.request
                with urllib.request.urlopen(f"{base}/api/health", timeout=2) as r:
                    healthy = json.loads(r.read()).get("ok") is True
                if healthy:
                    break
            except Exception:
                time.sleep(0.5)
        if not healthy:
            t.record(FAIL, "the dashboard starts and reports healthy",
                     f"/api/health never returned ok against {db}. "
                     f"Server log: {log}\n" + log.read_text(encoding="utf-8")[-800:])
            return
        t.record(OK, f"the dashboard starts and reports healthy on {base}")

        out = LOGS / "browser-check.json"
        p = subprocess.run([py, str(Path(__file__).parent / "browser_check.py"), base, project,
                            str(out)], capture_output=True, text=True, timeout=600)
        try:
            doc = json.loads(p.stdout)
        except ValueError:
            t.record(BLOCKED, "drive the dashboard with a real browser",
                     f"the browser driver produced no report (rc={p.returncode}):\n"
                     f"{(p.stderr or p.stdout)[-900:]}")
            return
        env_line = doc.get("env") or {}
        if env_line:
            info(f"browser: htmx={env_line.get('htmx_version')} "
                 f"htmx.swap={env_line.get('htmx_swap_fn')} "
                 f"startViewTransition={env_line.get('startViewTransition')}")
        for c in doc.get("checks", []):
            t.record({"OK": OK, "FAIL": FAIL, "BLOCKED": BLOCKED}[c["verdict"]],
                     c["label"], c.get("detail", ""))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


# ── /ledger and /receipts, answered by a stand-in for the session model ──────
LEDGER_TASK = """\
You are the model already in the session. The `/ledger` skill's instructions are
below, then the material `provledger ask` computed for one question. Do the part
the skill gives you: write the answer.

Answer ONLY from the material. Every sentence cites the record id it rests on.
Reproduce a computed absence sentence verbatim rather than writing your own, and
never fill a gap with a plausible explanation. Output the answer text and
nothing else: no preamble, no heading, no code fence.
"""

RECEIPTS_TASK = """\
You are the model already in the session. The `/receipts` skill's instructions
are below, then the material the CLI computed for one challenge. Do the part the
skill gives you: pick what the challenge is actually about and write the reply,
in the three sections the skill specifies and in that order.

Answer ONLY from the material. Output the three sections and nothing else.
"""


def ask_material(cli, project: str, question: str) -> tuple[int, dict]:
    """`/ledger` step 1 — the CLI fetches and computes; no model is involved."""
    rc, out = cli(["ask", question, "--project", project, "--json", "--no-model"])
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, {}


def receipts_material(t: Tally, cli, project: str, challenge: str) -> tuple[str, dict]:
    """`/receipts` steps 1 and 2. The skill specifies two reads with the model's
    judgement between them; where the CLI has not grown them yet, the older
    single-shot form is used and the gap is reported."""
    rc, out = cli(["receipts", "candidates", challenge, "--project", project, "--json"])
    if rc == 0:
        try:
            cand = json.loads(out)
        except ValueError:
            cand = {}
        qns = [c.get("qn") for c in (cand.get("candidates") or []) if c.get("qn")][:4]
        rc2, out2 = cli(["receipts", "facts", *qns, "--project", project, "--json"])
        if rc2 == 0:
            try:
                return "candidates+facts", json.loads(out2)
            except ValueError:
                pass
    t.finding("skills/receipts/SKILL.md runs `receipts candidates` and `receipts facts`, "
              "and the CLI provides neither",
              "`provledger receipts candidates …` is rejected by the argument parser, so the "
              "skill's documented flow — a broad candidate read, the model's pick, then the "
              "facts for those picks — cannot be followed. Fell back to the single-shot "
              "`receipts \"<challenge>\"`, in which a scoring function picks instead of the "
              "model; that is the very thing §8.8 point 1 replaces.")
    rc3, out3 = cli(["receipts", challenge, "--project", project, "--json"])
    try:
        return "single-shot", json.loads(out3)
    except ValueError:
        return "single-shot", {}


def material_text(doc: dict) -> str:
    """The material a model is given, as text: the fact table, the timeline, the
    computed absences and the scope line. Nothing added, nothing summarised."""
    bits = []
    if doc.get("facts_text"):
        bits.append("=== fact table ===\n" + doc["facts_text"])
    if doc.get("timeline"):
        bits.append("=== timeline, oldest first ===\n"
                    + "\n".join(f"- {x}" if isinstance(x, str) else f"- {json.dumps(x, default=str)}"
                                for x in doc["timeline"]))
    abs_ = doc.get("absences") or []
    if abs_:
        bits.append("=== computed absences (reproduce verbatim) ===\n"
                    + "\n".join("- " + (a.get("text") if isinstance(a, dict) else str(a))
                                for a in abs_))
    if doc.get("scope_line"):
        bits.append("=== scope ===\n" + doc["scope_line"])
    return "\n\n".join(bits)


def needle_check(t: Tally, q: dict, material: str, label: str) -> None:
    """Did the planted mistake reach the material at all?

    This is decidable by code and it is deliberately separate from stage 3's
    judging, because "the record was retrieved" and "the answer used it" are two
    different failures with two different fixes. A question that never names the
    record is the only kind that tests retrieval: `locate.candidates` matches
    words from the question, so its recall ceiling is the asker's vocabulary —
    and the asker does not know what is in there to ask for (accountability
    design §8.9).
    """
    needle = q.get("needle")
    if not needle:
        return
    kind = q.get("kind", "record")
    if needle.lower() in (material or "").lower():
        t.record(OK, f"{label}: the {kind} reached the material without being named",
                 f"the question does not contain {needle!r}, and the material does")
    else:
        t.record(FAIL, f"{label}: the {kind} reached the material without being named",
                 f"{needle!r} is nowhere in the {len(material)} characters the CLI computed. "
                 f"The question deliberately avoids the record's vocabulary, so this is a "
                 f"retrieval gap, not a wording problem: a recorded mistake is worth most "
                 f"exactly when nobody thought to ask for it.")


def answer_questions(t: Tally, cli, facts: dict, qs: list[dict]) -> list[dict]:
    project = facts["project"]
    out = []
    for q in qs:
        label = f"{q['id']} · {q['surface']}"
        if q["surface"] == "ask":
            rc, doc = ask_material(cli, project, q["question"])
            if rc != 0 or not doc:
                t.record(FAIL, f"{label}: the CLI computes the material",
                         f"`ask --no-model` exited {rc}")
                out.append({**q, "answer": "", "outcome": "material_failed"})
                continue
            mat = material_text(doc)
            prompt = (LEDGER_TASK + "\n===== skills/ledger/SKILL.md =====\n"
                      + SM.skill(UNDER_TEST, "ledger")
                      + f"\n\n===== the question =====\n{q['question']}\n\n"
                      + "===== the material =====\n" + mat + "\n")
            text, detail, outcome = SM.call(prompt, model=MODEL)
            checked = ""
            if outcome == "ok" and doc.get("ask_id") is not None:
                # the skill's step 3: the draft goes back through the same checks
                draft = ROOT / f"answer-{q['id']}.md"
                draft.write_text(text, encoding="utf-8")
                rc2, out2 = cli(["ask", "submit", str(doc["ask_id"]),
                                 "--answer-file", str(draft), "--json"])
                try:
                    sub = json.loads(out2)
                    checked = sub.get("answer") or ""
                    dropped = sub.get("dropped")
                    t.record(OK if rc2 == 0 else FAIL,
                             f"{label}: `ask submit` checks the draft",
                             f"{len(sub.get('sentences') or [])} sentence(s) kept, "
                             f"{dropped} dropped")
                except ValueError:
                    t.record(FAIL, f"{label}: `ask submit` checks the draft",
                             f"no JSON back (rc={rc2})")
            out.append({**q, "answer": checked or text, "raw_answer": text,
                        "material": mat, "outcome": outcome,
                        "runner_detail": detail, "ask_id": doc.get("ask_id"),
                        "scope_line": doc.get("scope_line")})
            needle_check(t, q, mat, label)
        else:
            form, doc = receipts_material(t, cli, project, q["question"])
            mat = material_text(doc)
            prompt = (RECEIPTS_TASK + "\n===== skills/receipts/SKILL.md =====\n"
                      + SM.skill(UNDER_TEST, "receipts")
                      + f"\n\n===== what the colleague said =====\n{q['question']}\n\n"
                      + "===== the material =====\n" + mat + "\n")
            text, detail, outcome = SM.call(prompt, model=MODEL)
            out.append({**q, "answer": text, "material": mat, "outcome": outcome,
                        "runner_detail": detail, "receipts_form": form,
                        "scope_line": doc.get("scope_line")})
            needle_check(t, q, mat, label)
        if out[-1]["outcome"] == "ok":
            t.record(OK, f"{label}: an answer came back",
                     f"{len(out[-1]['answer'].split())} words in "
                     f"{out[-1]['runner_detail'].get('elapsed_ms', '?')} ms")
        else:
            t.record(BLOCKED, f"{label}: an answer came back",
                     f"outcome {out[-1]['outcome']}: "
                     f"{out[-1]['runner_detail'].get('reason') or out[-1]['runner_detail'].get('result') or ''}")
    return out


# ── the stage ────────────────────────────────────────────────────────────────
def main() -> int:
    banner("STAGE 2 · a dummy project, then /ledger, /receipts and the dashboard")
    t = Tally()
    db = os.environ.get("ORCH_DB") or ""
    info(f"ledger under test: {db}")
    info(f"interpreter:       {PYTHON}")

    step("build a dummy project whose whole history we wrote")
    try:
        facts = DP.build(ROOT, UNDER_TEST, PYTHON, NONCE, say=info)
    except Exception as e:
        t.record(FAIL, "build the dummy project", f"{type(e).__name__}: {e}")
        write_verdict("2", t.verdict)
        return 0
    shape = facts["ledger_shape"]
    t.record(OK, "build the dummy project",
             f"{shape['plans']} plans · {shape['rows']} reason rows · "
             f"{shape['utterances']} utterances · {shape['references']} sources")
    # The story only supports the questions if these rows exist. Checked, not
    # assumed — the four kinds of recorded failure of accountability design §8.9
    # plus the absence the honest-absence questions rest on.
    for need, label in (
        (shape["rejected_paths"] >= 1,
         "kind 1/4 · a rejected path is on record"),
        (shape["anti_patterns"] >= 1,
         "kind 2/4 · a ledger entry with kind='anti_pattern' is on record"),
        (shape["expectations_observed"] >= 1,
         "kind 3/4 · an expectation has an observed outcome to contradict its claim"),
        (shape["failed_steps_in_completed_plans"] >= 1,
         "kind 4/4 · a FAILED step sits inside a plan that closed COMPLETED"),
        (shape["unstated"] >= 1,
         "at least one slot nobody ever explained is `unstated`"),
        (bool(facts.get("never_explained")),
         "the unexplained slot has a name a question can use"),
    ):
        t.record(OK if need else FAIL, label,
                 "" if need else "without it the questions that go looking for it prove nothing "
                                 "and stage 3's grading is meaningless")

    def cli(args: list[str], env: dict | None = None) -> tuple[int, str]:
        e = dict(os.environ)
        e["PYTHONPATH"] = os.pathsep.join(
            [str(UNDER_TEST / "orchestrator-backend"), e.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        for k, v in (env or {}).items():
            if v is None:            # None means "make sure this is not set"
                e.pop(k, None)
            else:
                e[k] = v
        p = subprocess.run(list(facts["cli_prefix"]) + args, cwd=str(facts["repo"]),
                           env=e, capture_output=True, text=True)
        with (LOGS / "stage2-cli.log").open("a", encoding="utf-8") as f:
            f.write(f"\n$ {' '.join(args)}\nrc={p.returncode}\n{p.stdout[:4000]}\n"
                    f"--stderr--\n{p.stderr[:2000]}\n")
        return p.returncode, p.stdout

    if facts["cli_form"].startswith("python -m"):
        t.finding("README documents `provledger ask` and `provledger receipts` as commands, "
                  "and the install produced no `provledger` on PATH",
                  f"used `{' '.join(facts['cli_prefix'][-3:])}` instead. "
                  "`[project.scripts] provledger` is declared in "
                  "orchestrator-backend/pyproject.toml, so an install that misses it leaves "
                  "every documented command line unrunnable as printed.")
    else:
        t.record(OK, f"the documented command exists: {facts['cli_form']}")

    qs = DP.questions(facts)
    # The answers below are written by a model handed these skills' own
    # instructions. A missing one is not a detail: it means the release under
    # test does not ship the flow this stage is checking.
    for name in ("ledger", "receipts"):
        body = SM.skill(UNDER_TEST, name)
        t.record(OK if len(body) > 500 else FAIL,
                 f"the release under test ships skills/{name}/SKILL.md",
                 f"{len(body)} characters" if body else
                 f"not present in {UNDER_TEST} — /{name} has no instructions to follow, so the "
                 f"answers below are not the ones the product would produce")

    runner_default_check(t, cli, facts["project"], qs[0]["question"])

    step("/ledger and /receipts, answered by a stand-in for the session model")
    ok, why = SM.available()
    if not ok:
        t.record(BLOCKED, "a model is reachable to write the answers",
                 f"{why}. /ledger and /receipts hand the writing to the session's own model; "
                 "with no session, this check stands in for it and cannot run without one. "
                 "Stage 3 has nothing to mark.")
        answers = []
    else:
        info(f"stand-in session model: {why}")
        answers = answer_questions(t, cli, facts, qs)

    dashboard_check(t, facts["project"], db)

    (ROOT / "stage3-input.json").write_text(
        json.dumps({"facts": facts, "answers": answers}, indent=1, default=str), encoding="utf-8")
    info(f"answers for stage 3: {ROOT / 'stage3-input.json'}")
    write_verdict("2", t.verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
