"""PreToolUse hook — the constraints anchored on what is about to be edited, as
additionalContext; never a veto unless a HUMAN constraint says block: true
(DP phase 2, Task 5; I8)."""
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator import constraints, db, hooks, psg_bridge

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def _feed(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A registered repo with pkg/m.py (load_orders lines 10–40, clean 50–60), a PSG that
    knows both, and an orchestrator DB with a human constraint on each."""
    repo = tmp_path / "repo"; (repo / "pkg").mkdir(parents=True)
    lines = [f"# line {i}" for i in range(1, 61)]
    lines[9] = "def load_orders(df):"; lines[10] = "    return df[df.paid]"
    lines[49] = "def clean(df):"; lines[50] = "    return df.dropna()"
    (repo / "pkg" / "m.py").write_text("\n".join(lines) + "\n")
    g = tmp_path / "proj-state-graph.db"
    c = ps.build(g); ps.add_run(c, 1, plan_id="P0")
    c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, line_start, line_end, struct_sig, dataflow_trivial, attrs_json) VALUES (1, 'nk_a', 'function', 'pkg.m.load_orders', 'pkg/m.py', 10, 40, 's', 1, '{}')")
    c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, line_start, line_end, struct_sig, dataflow_trivial, attrs_json) VALUES (1, 'nk_x', 'function', 'pkg.m.clean', 'pkg/m.py', 50, 60, 's', 1, '{}')")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
        INSERT INTO node_type (id, name) VALUES (1, 'function');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (7, 1, 'load_orders', 'pkg.m.load_orders', 'pkg/m.py', 1, 'nk_a');
        INSERT INTO consistency_card VALUES (7, '{"callers": [], "callees": [], "output_consumers": ["pkg.m.clean"], "dtype_map": {}, "lineage_downstream": []}');
    """)
    c.commit(); c.close()
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "proj", "repo": str(repo), "db_path": str(g), "commit_sha": "c"}]}))
    dbp = tmp_path / "orch.db"; errlog = tmp_path / "hook-errors.log"
    monkeypatch.setenv("ORCH_DB", str(dbp)); monkeypatch.setenv("PROVLEDGER_HOOK_ERRORS", str(errlog)); monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    conn = db.open_db(dbp); db.run_migrations(conn)
    ca = constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="keep paid orders only", rationale="SECRET finance reason", why_visibility="restricted")
    cx = constraints.record_constraint(conn, project="proj", subjects=["nk_x"], statement="clean must keep the label column", rationale="x")
    conn.close()
    return {"repo": repo, "db": dbp, "errlog": errlog, "reg": reg, "ca": ca, "cx": cx, "graph": g}


def _payload(world, tool="Edit", **ti):
    return {"session_id": "sess-9", "cwd": str(world["repo"]), "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": {"file_path": str(world["repo"] / "pkg" / "m.py"), **ti}}


def test_hit_injects_statements_not_rationale_and_writes_read_hits(world, monkeypatch, capsys):
    _feed(monkeypatch, _payload(world, old_string="    return df[df.paid]", new_string="    return df"))
    t0 = time.perf_counter()
    assert hooks.main(["PreToolUse"]) == 0
    assert time.perf_counter() - t0 < 0.5                                             # I8: the hook is cheap
    out = json.loads(capsys.readouterr().out)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and "permissionDecision" not in hso   # additive, never a veto by default
    ctx = hso["additionalContext"]
    assert "keep paid orders only" in ctx and "下游 pkg.m.clean: clean must keep the label column" in ctx
    assert "SECRET" not in ctx and "provledger why pkg.m.load_orders" in ctx and len(ctx) <= 700
    rows = sqlite3.connect(str(world["db"])).execute("SELECT reason_id, moment, session_id, injected_chars FROM read_hit ORDER BY reason_id").fetchall()
    assert rows == [(world["ca"], "edit", "sess-9", len(ctx)), (world["cx"], "edit", "sess-9", len(ctx))]
    assert not world["errlog"].exists()


def test_write_covers_the_whole_file_and_multiedit_each_range(world, monkeypatch, capsys):
    _feed(monkeypatch, _payload(world, tool="Write", content="x"))
    hooks.main(["PreToolUse"])
    assert "keep paid orders only" in json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    _feed(monkeypatch, _payload(world, tool="MultiEdit", edits=[{"old_string": "# line 45", "new_string": "y"}, {"old_string": "def clean(df):", "new_string": "def clean(d):"}]))
    hooks.main(["PreToolUse"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "clean must keep the label column" in ctx and "keep paid orders only" not in ctx


def test_no_anchored_node_or_other_tool_means_not_one_byte(world, monkeypatch, capsys):
    _feed(monkeypatch, _payload(world, old_string="# line 45", new_string="z"))            # a gap between the two nodes
    assert hooks.main(["PreToolUse"]) == 0 and capsys.readouterr().out == ""
    _feed(monkeypatch, _payload(world, old_string="not in the file", new_string="z"))
    assert hooks.main(["PreToolUse"]) == 0 and capsys.readouterr().out == ""
    _feed(monkeypatch, {**_payload(world, old_string="def load_orders(df):", new_string="q"), "tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert hooks.main(["PreToolUse"]) == 0 and capsys.readouterr().out == ""
    assert sqlite3.connect(str(world["db"])).execute("SELECT COUNT(*) FROM read_hit").fetchone()[0] == 0


def test_a_file_outside_every_registered_repo_opens_nothing(world, monkeypatch, capsys, tmp_path):
    other = tmp_path / "elsewhere" / "m.py"; other.parent.mkdir(); other.write_text("def load_orders(df):\n    return df\n")
    def boom(*a, **k):
        raise AssertionError("the PSG must not be opened for a file outside the registry")
    monkeypatch.setattr(psg_bridge, "_query", boom); monkeypatch.setattr(psg_bridge, "open_ro", boom)
    monkeypatch.setattr(hooks, "_open", boom)                                            # nor the orchestrator DB
    _feed(monkeypatch, {"session_id": "s", "cwd": str(other.parent), "hook_event_name": "PreToolUse", "tool_name": "Edit",
                        "tool_input": {"file_path": str(other), "old_string": "def load_orders(df):", "new_string": "x"}})
    assert hooks.main(["PreToolUse"]) == 0 and capsys.readouterr().out == "" and not world["errlog"].exists()


def test_block_true_human_constraint_is_the_only_deny(world, monkeypatch, capsys):
    (world["repo"] / "provledger-extensions.json").write_text(json.dumps({"version": 1, "constraints": [
        {"statement": "keep paid orders only", "subjects": ["pkg.m.load_orders"], "block": True}]}))
    _feed(monkeypatch, _payload(world, old_string="    return df[df.paid]", new_string="    return df"))
    hooks.main(["PreToolUse"])
    hso = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and "block: true" in hso["permissionDecisionReason"] and "keep paid orders only" in hso["additionalContext"]
    # a SYSTEM constraint with the same words never denies
    conn = db.open_db(world["db"])
    from orchestrator import provenance as pv
    pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_x", kind="organizational", role="constraint", statement="keep paid orders only", recorded_by="system", rule_id="constraint_bypassed")
    conn.close()
    _feed(monkeypatch, _payload(world, old_string="def clean(df):", new_string="def clean(d):"))
    hooks.main(["PreToolUse"])
    assert "permissionDecision" not in json.loads(capsys.readouterr().out)["hookSpecificOutput"]


def test_an_error_is_one_log_line_and_an_empty_stdout(world, monkeypatch, capsys):
    monkeypatch.setattr(hooks, "anchor_context", lambda conn, data: (_ for _ in ()).throw(RuntimeError("boom")))
    _feed(monkeypatch, _payload(world, old_string="def load_orders(df):", new_string="q"))
    assert hooks.main(["PreToolUse"]) == 0 and capsys.readouterr().out == ""
    lines = world["errlog"].read_text().splitlines()
    assert len(lines) == 1 and "PreToolUse" in lines[0] and "RuntimeError: boom" in lines[0]


def test_the_shell_hook_passes_the_json_through_and_hooks_json_registers_it(world):
    env = dict(os.environ, ORCH_DB=str(world["db"]), PROVLEDGER_HOOK_ERRORS=str(world["errlog"]), PSG_REGISTRY_PATH=str(world["reg"]),
               CLAUDE_PLUGIN_ROOT=str(REPO))
    r = subprocess.run(["bash", str(REPO / "hooks" / "anchor_check.sh")], input=json.dumps(_payload(world, old_string="def load_orders(df):", new_string="q")),
                       capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    r = subprocess.run(["bash", str(REPO / "hooks" / "anchor_check.sh")], input=json.dumps(_payload(world, old_string="# line 45", new_string="q")),
                       capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode == 0 and r.stdout == ""
    cfg = json.loads((REPO / "hooks" / "hooks.json").read_text())
    pre = cfg["hooks"]["PreToolUse"][0]
    assert pre["matcher"] == "Edit|Write|MultiEdit" and pre["hooks"][0]["command"].endswith('hooks/anchor_check.sh"')
