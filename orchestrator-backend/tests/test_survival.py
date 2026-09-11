"""orchestrator.survival — the weak, derived signal after an expectation (spec §3.3)."""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import survival

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

SINCE = "2026-09-11T00:30:00+00:00"          # the expectation was made between run 1 and run 2


def _graph(tmp_path, events):
    """run 1 (before SINCE): nk_a added. Later runs (after SINCE): `events` = [(run_id, plan_id, event_type, payload)]."""
    path = tmp_path / "g.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_snapshot(c, 1, "nk_a", "pkg.m.load")
    ps.add_event(c, 1, 1, "node_added", "nk_a", created_at="2026-09-11T00:00:01+00:00")
    seq = {}
    for run_id, plan_id, et, payload in events:
        if not c.execute("SELECT 1 FROM analysis_run WHERE id=?", (run_id,)).fetchone():
            ps.add_run(c, run_id, plan_id=plan_id)
            ps.add_snapshot(c, run_id, "nk_a", "pkg.m.load")
        seq[run_id] = seq.get(run_id, 0) + 1
        ps.add_event(c, run_id, seq[run_id], et, "nk_a", json.dumps(payload), created_at=f"2026-09-11T0{run_id}:00:00+00:00")
    c.commit(); c.close()
    return str(path)


def test_removed(tmp_path):
    g = _graph(tmp_path, [(2, "P1", "node_removed", {})])
    out = survival.derive(g, "pkg.m.load", SINCE, consumers=["x"])
    assert out["signal"] == "removed" and len(out["evidence"]) == 1 and "weak" in out["caveat"]


def test_reverted(tmp_path):
    g = _graph(tmp_path, [(2, "P1", "node_changed", {"changed": ["struct_sig"], "struct_sig": {"from": "s0", "to": "s1"}}),
                          (3, "P2", "node_changed", {"changed": ["struct_sig"], "struct_sig": {"from": "s1", "to": "s0"}})])
    out = survival.derive(g, "pkg.m.load", SINCE)
    assert out["signal"] == "reverted" and len(out["evidence"]) == 2


def test_churned(tmp_path):
    g = _graph(tmp_path, [(2, "P1", "node_changed", {"changed": ["struct_sig"], "struct_sig": {"from": "s0", "to": "s1"}}),
                          (3, "P2", "node_changed", {"changed": ["struct_sig"], "struct_sig": {"from": "s1", "to": "s2"}})])
    out = survival.derive(g, "pkg.m.load", SINCE)
    assert out["signal"] == "churned" and out["plans"] == ["P1", "P2"]


def test_untouched_consumed_and_untouched(tmp_path):
    g = _graph(tmp_path, [(2, "P1", "node_matched", {"via": "qualname"})])
    assert survival.derive(g, "pkg.m.load", SINCE, consumers=["pkg.m.report"])["signal"] == "untouched_consumed"
    assert survival.derive(g, "pkg.m.load", SINCE, consumers=[])["signal"] == "untouched"
    # events BEFORE the expectation do not count
    (tmp_path / "b").mkdir()
    g2 = _graph(tmp_path / "b", [])
    assert survival.derive(g2, "pkg.m.load", SINCE)["signal"] == "untouched"


def test_unknown_when_graph_or_node_missing(tmp_path):
    assert survival.derive(str(tmp_path / "absent.db"), "pkg.m.load", SINCE)["signal"] == "unknown"
    g = _graph(tmp_path, [])
    out = survival.derive(g, "pkg.m.nope", SINCE)
    assert out["signal"] == "unknown" and "not in the state graph" in out["reason"]
