"""review_run.py — every subprocess has a ceiling, and a timeout closes the step.

A4 of the accountability spec: the review step rebuilds the whole graph and runs
the project's own test suite, so it is minutes long. Without `timeout=` a hung
suite leaves REVIEW.1 IN_PROGRESS forever and the plan says nothing — a tool that
gets slower in silence is the thing this project exists to prevent.

Most of these tests are deliberately NOT `live` and touch no git repo and no DB:
the driver runs in-process with `subprocess.run` replaced by a recorder, so the
assertion is about the kwargs the driver passes and the path a TimeoutExpired
takes, not about how fast this machine happens to be.

The exceptions are the two process-group tests at the bottom. A ceiling that
kills only the `bash` it started is not a ceiling: `init_project.sh` spawns the
analyzer, so a refresh that runs out of time could leave that analyzer running
and still writing to the state-graph database — and the NEXT run would take that
half-written graph as its predecessor for identity matching, which is how `nk_`
keys get inherited. Those two tests therefore let ONE call site run for real,
against a command that spawns a child which would outlive its parent, and assert
the child is gone. They use a 1-second ceiling, so they are fast, but they are
the only ones here that touch a real process.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
SKILL_MD = SCRIPTS.parent / "SKILL.md"
sys.path.insert(0, str(SCRIPTS))

import review_run  # noqa: E402

PLAN = "rv-1"
CHILD = f"{PLAN}-REVIEW.1"


def _kind(args, kw) -> str:
    """Which of the three call sites this is: tests | graph | <write script>."""
    if kw.get("shell"):
        return "tests"
    parts = [str(x) for x in args]
    if any(p.endswith("init_project.sh") for p in parts):
        return "graph"
    return Path(parts[1]).stem if len(parts) > 1 else "?"


REAL_RUN = subprocess.run          # captured before any monkeypatching


class Recorder:
    """Stands in for subprocess.run: records (kind, kwargs, payload) and can raise
    TimeoutExpired at one chosen call site.

    `live_on` lets exactly one call site through to the real `subprocess.run` with
    the kwargs the driver chose — which is how a test can ask what actually happens
    to a real process tree when the ceiling is reached."""

    def __init__(self, timeout_on: str | None = None, live_on: str | None = None):
        self.timeout_on = timeout_on
        self.live_on = live_on
        self.calls: list[dict] = []

    def __call__(self, args, **kw):
        kind = _kind(args, kw)
        payload = {}
        if kind not in ("tests", "graph"):
            try:
                payload = json.loads(Path(str(args[2])).read_text())
            except (OSError, ValueError, IndexError):
                payload = {}
        self.calls.append({"kind": kind, "kwargs": kw, "payload": payload, "args": args})
        if kind == self.live_on:
            return REAL_RUN(args, **kw)          # raises TimeoutExpired itself, for real
        if kind == self.timeout_on:
            raise subprocess.TimeoutExpired(cmd=args, timeout=kw.get("timeout"))
        if kind == "reason-slots":
            stdout = json.dumps({"slots": [], "checklist": "(no slots)"}, indent=1) + "\n" + json.dumps({"ok": True, "op": kind}) + "\n"
        else:
            stdout = json.dumps({"ok": True, "op": kind}) + "\n"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    def of(self, kind: str) -> list[dict]:
        return [c for c in self.calls if c["kind"] == kind]

    @property
    def kinds(self) -> list[str]:
        return [c["kind"] for c in self.calls]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Run review_run.main() in-process on a green review: no git, no sqlite, no
    write scripts — only the kwargs the driver hands to subprocess.run."""
    graph_db = tmp_path / "graph.db"
    graph_db.write_text("")
    entry = {"name": "proj", "repo": str(tmp_path / "repo"), "db_path": str(graph_db),
             "commit_sha": "a" * 40, "updated_at": "2026-09-27T00:00:00Z"}

    monkeypatch.setattr(review_run.Driver, "registry_entry", lambda self: dict(entry))

    def fake_read(self, sql, *params):
        if "FROM Steps" in sql:
            return [("PENDING", None)]
        if "FROM Plans" in sql:
            return [("COMPLETED",)]
        return []

    monkeypatch.setattr(review_run.Driver, "read", fake_read)
    monkeypatch.setattr(review_run.review_diff, "_git",
                        lambda repo, *args: ("b" * 40 + "\n") if args[0] == "rev-parse" else "")
    monkeypatch.setattr(review_run.review_diff, "resolve_range",
                        lambda repo, sha: ("a" * 40, "HEAD", "sha..HEAD"))
    monkeypatch.setattr(review_run.review_diff, "changed_symbols", lambda repo, base, head: [])
    monkeypatch.setattr(review_run.review_diff.contract_diff, "changed_files",
                        lambda repo, base, head: ["pipeline.py"])
    monkeypatch.setattr(review_run.review_diff, "full_verdict",
                        lambda *a, **k: {"ok": True, "gates": {"stale_references": True}, "gaps": {}, "text": "verdict OK"})
    monkeypatch.setattr(review_run.selfcheck, "run", lambda db: {"ok": True, "report": "[OK  ] graph\n"})

    def run(*extra, timeout_on=None, live_on=None):
        rec = Recorder(timeout_on, live_on=live_on)
        monkeypatch.setattr(review_run.subprocess, "run", rec)
        argv = ["review_run.py", "--plan-id", PLAN, "--project", "proj",
                "--tests", "pytest -q", "--json", *extra]
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as e:
            review_run.main()
        return rec, (e.value.code if isinstance(e.value.code, int) else 1)

    return run


# ── (a) every call site has a ceiling ────────────────────────────────────────

def test_all_three_call_sites_pass_a_timeout(harness):
    rec, code = harness()
    assert code == 0, rec.kinds
    assert rec.of("graph") and rec.of("tests"), rec.kinds
    writes = [c for c in rec.calls if c["kind"] not in ("graph", "tests")]
    assert {"append-log", "start-step", "reason-slots", "complete-step"} <= {c["kind"] for c in writes}, rec.kinds
    for c in rec.calls:
        assert isinstance(c["kwargs"].get("timeout"), (int, float)), f"{c['kind']} runs without a timeout"
        assert c["kwargs"]["timeout"] > 0


# ── (b) the defaults are the numbers SKILL.md publishes ──────────────────────

def test_every_call_site_gets_its_own_ceiling(harness):
    rec, code = harness()
    assert code == 0
    assert {c["kwargs"]["timeout"] for c in rec.calls if c["kind"] not in ("graph", "tests")} == {60}
    assert rec.of("graph")[0]["kwargs"]["timeout"] == review_run.DEFAULT_TIMEOUT_GRAPH_S
    assert rec.of("tests")[0]["kwargs"]["timeout"] == review_run.DEFAULT_TIMEOUT_TESTS_S


# The slowest graph refresh this repository has on record, read from its own
# analysis_run table: ten runs between 795 and 3085 seconds, median 869, on a
# 6710-node graph. The first version of this constant was 300 — below every run
# that table holds — and it auto-failed the first review it ever ran against.
# So the assertion is not "the number is 4800"; it is "the number clears what
# this project actually takes", which is the thing that was got wrong.
SLOWEST_RECORDED_REFRESH_S = 3085


def test_the_graph_ceiling_clears_the_slowest_refresh_on_record():
    """A ceiling is there to catch a hang, not a large repository. Pinning only
    the literal would have let the original 300 through; pinning the margin is
    what catches the mistake that was actually made."""
    assert review_run.DEFAULT_TIMEOUT_GRAPH_S > SLOWEST_RECORDED_REFRESH_S, (
        f"the graph ceiling ({review_run.DEFAULT_TIMEOUT_GRAPH_S}s) is below a refresh this "
        f"project has already recorded ({SLOWEST_RECORDED_REFRESH_S}s) — every review would fail")
    assert review_run.DEFAULT_TIMEOUT_GRAPH_S < 4 * SLOWEST_RECORDED_REFRESH_S, \
        "a ceiling that generous stops being a ceiling"


def test_the_constants_are_named_and_documented():
    assert review_run.TIMEOUT_WRITE_S == 60
    assert review_run.DEFAULT_TIMEOUT_TESTS_S == 600
    md = SKILL_MD.read_text(encoding="utf-8")
    for needle, value in (("--timeout-tests", str(review_run.DEFAULT_TIMEOUT_TESTS_S)),
                          ("--timeout-graph", str(review_run.DEFAULT_TIMEOUT_GRAPH_S))):
        lines = [ln for ln in md.splitlines() if needle in ln and value in ln]
        assert lines, f"SKILL.md must publish {needle} = {value}s"
    assert [ln for ln in md.splitlines() if "60" in ln and "write" in ln.lower()], \
        "SKILL.md must publish the 60s ceiling on the write scripts"


# ── (c) + (d) a timeout FAILS the step, out loud ─────────────────────────────

def test_a_test_command_timeout_fails_the_step_with_the_reason(harness):
    rec, code = harness(timeout_on="tests")
    assert code == 1, rec.kinds
    fails = rec.of("fail-step")
    assert len(fails) == 1, rec.kinds
    reason = fails[0]["payload"]["reason"]
    assert "timeout" in reason.lower(), reason
    assert re.search(r"\d+(\.\d+)?s", reason), f"the reason must carry the elapsed seconds: {reason}"
    assert "600" in reason, f"the reason must name the ceiling it hit: {reason}"
    assert not rec.of("complete-step"), "a timed-out review must never complete the step"


def test_a_timeout_never_leaves_the_plan_silently_in_progress(harness, capsys):
    rec, code = harness(timeout_on="tests")
    assert code == 1
    fails = rec.of("fail-step")
    assert fails[0]["payload"]["step_id"] == CHILD, fails[0]["payload"]
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["closed"] == "FAILED", result


def test_a_graph_refresh_timeout_fails_the_step_too(harness):
    rec, code = harness(timeout_on="graph")
    assert code == 1, rec.kinds
    reason = rec.of("fail-step")[0]["payload"]["reason"]
    assert "timeout" in reason.lower(), reason
    assert str(review_run.DEFAULT_TIMEOUT_GRAPH_S) in reason, \
        f"the reason must name the ceiling it hit, not a number someone typed here: {reason}"
    assert not rec.of("tests"), "the test suite must not run after the refresh timed out"


def test_a_write_script_timeout_is_a_write_path_failure(harness):
    rec, code = harness(timeout_on="append-log")
    assert code == 5, rec.kinds          # same exit as any other failing write script


# ── (e) the ceilings are overridable from the command line ───────────────────

def test_the_timeouts_are_overridable(harness):
    rec, code = harness("--timeout-tests", "7", "--timeout-graph", "11")
    assert code == 0, rec.kinds
    assert rec.of("tests")[0]["kwargs"]["timeout"] == 7
    assert rec.of("graph")[0]["kwargs"]["timeout"] == 11


# ── (f) a ceiling that kills only `bash` is not a ceiling ────────────────────

def _alive(pid: int) -> bool:
    """Is this pid still there? Signal 0 asks without sending anything."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                      # somebody else's now — still a process
        return True
    return True


def _gone_within(pid: int, seconds: float = 5.0) -> bool:
    """SIGKILL is immediate but reaping by init is not, so poll rather than guess."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _reap(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def test_the_two_long_call_sites_lead_their_own_process_group(harness):
    """`start_new_session=True` is what makes a group kill possible at all. The
    write scripts are one sqlite write each and are left where they are."""
    rec, code = harness()
    assert code == 0, rec.kinds
    assert rec.of("graph")[0]["kwargs"].get("start_new_session") is True
    assert rec.of("tests")[0]["kwargs"].get("start_new_session") is True


def test_a_timed_out_test_command_leaves_no_surviving_descendant(harness, tmp_path):
    """The test command spawns a child that would outlive it. After the ceiling is
    reached, that child must be gone — not orphaned and still running."""
    (tmp_path / "repo").mkdir(exist_ok=True)      # the test command runs IN the repo
    pidfile = tmp_path / "tests-orphan.pid"
    cmd = f"sleep 45 & echo $! > {pidfile}; sleep 45"
    rec, code = harness("--tests", cmd, "--timeout-tests", "1", live_on="tests")
    assert code == 1, rec.kinds
    reason = rec.of("fail-step")[0]["payload"]["reason"]
    assert "timeout" in reason.lower() and "1s ceiling" in reason, reason
    assert re.search(r"elapsed \d+(\.\d+)?s", reason), reason
    pid = int(pidfile.read_text().strip())
    try:
        assert _gone_within(pid), f"pid {pid} outlived the timeout that was supposed to stop it"
    finally:
        _reap(pid)


def test_a_timed_out_graph_refresh_leaves_no_surviving_analyzer(harness, tmp_path, monkeypatch):
    """The case this exists for: init_project.sh spawns the analyzer, which writes
    to the state-graph database. A refresh that runs out of time must not leave it
    writing — the next run would inherit its half-written graph as a predecessor."""
    pidfile = tmp_path / "graph-orphan.pid"
    fake = tmp_path / "psg"
    fake.mkdir()
    script = fake / "init_project.sh"                 # the name _kind() recognises
    script.write_text(f"#!/bin/bash\nsleep 45 &\necho $! > {pidfile}\nsleep 45\n")
    script.chmod(0o755)
    monkeypatch.setattr(review_run, "INIT_PROJECT", script)
    rec, code = harness("--timeout-graph", "1", live_on="graph")
    assert code == 1, rec.kinds
    reason = rec.of("fail-step")[0]["payload"]["reason"]
    assert "timeout" in reason.lower() and "1s ceiling" in reason, reason
    assert not rec.of("tests"), "the test suite must not run after the refresh timed out"
    pid = int(pidfile.read_text().strip())
    try:
        assert _gone_within(pid), f"the analyzer stand-in {pid} outlived the refresh ceiling"
    finally:
        _reap(pid)


def test_a_timed_out_write_reports_what_the_ledger_shows_afterwards(harness, monkeypatch, capsys):
    """FL-211. complete-step REVIEW.1 outlived its ceiling while the step had in
    fact been written; the driver said "nothing it would have written is
    confirmed", which was false, and left nothing to go on. It now reads the step
    and the plan back and says what they show."""
    def read_after(self, sql, *params):
        wrote = any(c["kind"] == "complete-step" for c in getattr(subprocess.run, "calls", []))
        if "FROM Steps WHERE step_id" in sql:
            return [("COMPLETED",)] if wrote else [("PENDING",)]
        if "review_state FROM Plans" in sql:
            return [("IN_PROGRESS", "awaiting_agent")]
        if "FROM Steps" in sql:
            return [("PENDING", None)]
        if "FROM Plans" in sql:
            return [("COMPLETED",)]
        return []

    monkeypatch.setattr(review_run.Driver, "read", read_after)
    rec, code = harness(timeout_on="complete-step")
    assert code == 5
    said = capsys.readouterr()
    text = said.err + said.out
    assert "nothing it would have written is confirmed" not in text
    assert f"{CHILD} reads COMPLETED" in text and "IN_PROGRESS" in text, text[-600:]
    assert "the close did not finish" in text
