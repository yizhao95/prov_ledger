"""The degraded mode — hooks alone (DP phase 2, Task 7b). One simulated session
with no provledger skill in it: UserPromptSubmit + PostToolUse + one Edit's
PreToolUse + Stop, then the background refresh reporting back. Every loss
without the skills is asserted as a visible field, never inferred from silence."""
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from orchestrator import db, hooks, psg_bridge

try:
    from orchestrator import session
except ImportError:                       # RED: the module does not exist yet
    session = None

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

pytestmark = pytest.mark.degraded


def _feed(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A registered repo (a git repo, so HEAD exists) with pkg/m.py, a graph knowing
    load_orders (lines 10–40) and one human constraint on it."""
    import subprocess
    repo = tmp_path / "repo"; (repo / "pkg").mkdir(parents=True)
    lines = [f"# line {i}" for i in range(1, 41)]; lines[9] = "def load_orders(df):"; lines[10] = "    return df[df.paid]"
    (repo / "pkg" / "m.py").write_text("\n".join(lines) + "\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    gdir = tmp_path / "graphs" / "proj"; gdir.mkdir(parents=True)
    g = gdir / "proj-state-graph.db"
    c = ps.build(g); ps.add_run(c, 1, plan_id="P0", sha=sha)
    c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, line_start, line_end, struct_sig, dataflow_trivial, attrs_json) VALUES (1, 'nk_a', 'function', 'pkg.m.load_orders', 'pkg/m.py', 10, 40, 's', 1, '{}')")
    ps.add_event(c, 1, 1, "node_added", "nk_a")
    c.commit(); c.close()
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "proj", "repo": str(repo), "db_path": str(g), "commit_sha": sha}]}))
    dbp = tmp_path / "orch.db"; errlog = tmp_path / "hook-errors.log"
    monkeypatch.setenv("ORCH_DB", str(dbp)); monkeypatch.setenv("PROVLEDGER_HOOK_ERRORS", str(errlog)); monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    conn = db.open_db(dbp); db.run_migrations(conn)
    from orchestrator import constraints
    cid = constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="keep paid orders only", rationale="finance")
    conn.close()
    spawned = []
    monkeypatch.setattr(session, "_spawn", lambda argv, log: (spawned.append(argv), 4242)[1])
    return {"repo": repo, "db": dbp, "errlog": errlog, "graph": g, "gdir": gdir, "sha": sha, "cid": cid, "spawned": spawned, "reg": reg}


def _session(world, monkeypatch, capsys, sid="sess-d1", edit=True):
    """The three hooks a skill-less session fires, verbatim shapes."""
    _feed(monkeypatch, {"session_id": sid, "cwd": str(world["repo"]), "hook_event_name": "UserPromptSubmit", "prompt": "make load_orders keep only paid rows"})
    hooks.main(["UserPromptSubmit"])
    _feed(monkeypatch, {"session_id": sid, "cwd": str(world["repo"]), "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "pytest -q"}})
    hooks.main(["PostToolUse"])
    if edit:
        _feed(monkeypatch, {"session_id": sid, "cwd": str(world["repo"]), "hook_event_name": "PreToolUse", "tool_name": "Edit",
                            "tool_input": {"file_path": str(world["repo"] / "pkg" / "m.py"), "old_string": "    return df[df.paid]", "new_string": "    return df[df.paid & df.shipped]"}})
        hooks.main(["PreToolUse"])
        out = capsys.readouterr().out
        assert "keep paid orders only" in out                      # the hook still speaks without any skill
    (world["repo"] / "pkg" / "m.py").write_text((world["repo"] / "pkg" / "m.py").read_text().replace("df[df.paid]", "df[df.paid & df.shipped]"))
    _feed(monkeypatch, {"session_id": sid, "cwd": str(world["repo"]), "hook_event_name": "Stop", "stop_hook_active": False})
    hooks.main(["Stop"])
    assert capsys.readouterr().out == ""                              # Stop never writes stdout


def test_hooks_only_session_queues_a_refresh_and_records_what_it_saw(world, monkeypatch, capsys):
    _session(world, monkeypatch, capsys)
    c = sqlite3.connect(str(world["db"]))
    assert c.execute("SELECT session_id FROM utterance").fetchall() == [("sess-d1",)]
    assert c.execute("SELECT session_id, command_head FROM tool_call_log").fetchall() == [("sess-d1", "pytest -q")]
    assert c.execute("SELECT moment, session_id, injected_chars > 0 FROM read_hit").fetchall() == [("edit", "sess-d1", 1)]
    row = session.get(c, "sess-d1")
    assert row["project"] == "proj" and row["refresh_state"] == "queued" and "tracked files changed" in row["note"]
    assert (world["gdir"] / ".refresh.lock").exists()
    argv = world["spawned"][0]
    assert argv[1].endswith("init_project.sh") and "--trigger" in argv and argv[argv.index("--trigger") + 1] == "session"
    assert argv[argv.index("--session-id") + 1] == "sess-d1" and argv[argv.index("--notify-orch-db") + 1] == str(world["db"])
    assert not world["errlog"].exists()


def test_a_session_with_a_plan_skips_the_refresh(world, monkeypatch, capsys):
    c = db.open_db(world["db"])
    c.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES ('P7', 'g', 'IN_PROGRESS', 'proj', 'declared', strftime('%Y-%m-%d %H:%M:%S','now'))")
    c.commit(); c.close()
    _session(world, monkeypatch, capsys, sid="sess-d2", edit=False)
    row = session.get(sqlite3.connect(str(world["db"])), "sess-d2")
    assert row["refresh_state"] == "skipped" and row["note"].startswith("plans in session: P7") and world["spawned"] == []


def test_unchanged_tree_off_switch_and_a_running_refresh_all_skip_with_a_reason(world, monkeypatch, capsys, tmp_path):
    _feed(monkeypatch, {"session_id": "s-clean", "cwd": str(world["repo"]), "hook_event_name": "Stop"}); hooks.main(["Stop"])
    c = sqlite3.connect(str(world["db"]))
    assert session.get(c, "s-clean")["note"] == "tree unchanged since the graph was built"
    (world["repo"] / "provledger-extensions.json").write_text(json.dumps({"version": 1, "reasons": {"session_refresh": "off"}}))
    (world["repo"] / "pkg" / "m.py").write_text("changed\n")
    _feed(monkeypatch, {"session_id": "s-off", "cwd": str(world["repo"]), "hook_event_name": "Stop"}); hooks.main(["Stop"])
    assert "session_refresh is off" in session.get(c, "s-off")["note"]
    (world["repo"] / "provledger-extensions.json").unlink()
    (world["gdir"] / ".refresh.lock").write_text("other\n")
    _feed(monkeypatch, {"session_id": "s-busy", "cwd": str(world["repo"]), "hook_event_name": "Stop"}); hooks.main(["Stop"])
    assert "already running" in session.get(c, "s-busy")["note"]
    _feed(monkeypatch, {"session_id": "s-out", "cwd": str(tmp_path), "hook_event_name": "Stop"}); hooks.main(["Stop"])
    assert session.get(c, "s-out")["note"] == "cwd is not inside a registered project" and session.get(c, "s-out")["project"] is None
    assert world["spawned"] == [] and capsys.readouterr().out == ""


def _refresh_run(world, sid="sess-d1"):
    """What init_project.sh --trigger session leaves behind: a run attributed to session:<sid> with the edit."""
    c = sqlite3.connect(str(world["graph"]))
    c.execute("INSERT INTO analysis_run (id, project_name, commit_sha, started_at, plan_id, trigger) VALUES (2, 'proj', ?, '2026-09-16T00:00:00+00:00', ?, 'session')", (world["sha"], session.session_plan_id(sid)))
    c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, line_start, line_end, struct_sig, dataflow_trivial, attrs_json) VALUES (2, 'nk_a', 'function', 'pkg.m.load_orders', 'pkg/m.py', 10, 40, 's2', 1, '{}')")
    ps.add_event(c, 2, 1, "node_changed", "nk_a", '{"changed": ["struct_sig"]}')
    c.commit(); c.close()


def test_after_the_refresh_the_rules_run_and_what_is_lost_is_explicit(world, monkeypatch, capsys):
    _session(world, monkeypatch, capsys)
    _refresh_run(world)
    conn = db.open_db(world["db"])
    out = session.refreshed(conn, session_id="sess-d1", state="done", psg_db_path=str(world["graph"]))
    assert out["refresh_state"] == "done" and out["psg_run_id"] == 2 and "evaluation" in out, out
    row = session.get(conn, "sess-d1")
    assert row["refresh_state"] == "done" and row["psg_run_id"] == 2 and not (world["gdir"] / ".refresh.lock").exists()
    assert psg_bridge._query(str(world["graph"]), "SELECT trigger FROM analysis_run WHERE id = 2")[0][0] == "session"
    pid = session.session_plan_id("sess-d1")
    tiers = {r[0] for r in conn.execute("SELECT tier FROM change_reason WHERE plan_id = ? AND role = 'reason'", (pid,))}
    assert tiers and tiers <= {"derived", "stated", "unstated"}                          # never asserted: nobody interpreted anything
    assert "stated" in tiers                                                             # R0: "make load_orders keep only paid rows" names the node
    assert conn.execute("SELECT COUNT(*) FROM headline WHERE session_id = 'sess-d1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM influence").fetchone()[0] == 0              # nothing was adopted — nobody answered
    assert conn.execute("SELECT COUNT(*) FROM headline_response").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM read_hit WHERE moment = 'edit'").fetchone()[0] == 1   # the one thing shown: the hook's injection
    assert conn.execute("SELECT COUNT(*) FROM read_hit WHERE moment = 'close'").fetchone()[0] == 0  # the session headline showed nobody anything
    conn.close()


def test_a_failed_refresh_is_recorded_and_the_lock_released(world, monkeypatch, capsys):
    _session(world, monkeypatch, capsys)
    conn = db.open_db(world["db"])
    out = session.refreshed(conn, session_id="sess-d1", state="failed")
    assert out["refresh_state"] == "failed" and session.get(conn, "sess-d1")["refresh_state"] == "failed"
    assert not (world["gdir"] / ".refresh.lock").exists() and conn.execute("SELECT COUNT(*) FROM headline").fetchone()[0] == 0


def test_hooks_json_registers_stop_and_the_shell_hook_stays_silent(world):
    import os, subprocess
    repo = Path(__file__).resolve().parents[2]
    cfg = json.loads((repo / "hooks" / "hooks.json").read_text())
    assert cfg["hooks"]["Stop"][0]["hooks"][0]["command"].endswith('hooks/session_close.sh"')
    env = dict(os.environ, ORCH_DB=str(world["db"]), PROVLEDGER_HOOK_ERRORS=str(world["errlog"]), PSG_REGISTRY_PATH=str(world["reg"]), CLAUDE_PLUGIN_ROOT=str(repo))
    r = subprocess.run(["bash", str(repo / "hooks" / "session_close.sh")], input=json.dumps({"session_id": "sh-1", "cwd": "/nowhere"}), capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and r.stdout == ""
    assert session.get(sqlite3.connect(str(world["db"])), "sh-1")["note"] == "cwd is not inside a registered project"
