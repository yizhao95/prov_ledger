"""Tests for the parallel-step detection + tree-building helpers in queries.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.queries import detect_parallel_groups, build_step_tree


def _step(step_id: str, parent: str | None = None, depth: int = 0,
          start: str | None = None, end: str | None = None,
          status: str = "COMPLETED", description: str = "x", order: int = 1,
          step_type: str = "CODE") -> dict:
    """Minimal step dict matching the Steps schema columns the helpers use."""
    return {
        "step_id": step_id,
        "parent_step_id": parent,
        "depth_level": depth,
        "started_at": start,
        "completed_at": end,
        "status": status,
        "description": description,
        "execution_order": order,
        "step_type": step_type,
    }


# ── detect_parallel_groups ────────────────────────────────────────────────

class TestDetectParallel:
    def test_no_overlap_all_alone(self):
        sibs = [
            _step("a", start="2026-01-01 10:00:00", end="2026-01-01 10:01:00"),
            _step("b", start="2026-01-01 10:01:01", end="2026-01-01 10:02:00"),
            _step("c", start="2026-01-01 10:02:01", end="2026-01-01 10:03:00"),
        ]
        groups = detect_parallel_groups(sibs)
        assert groups == {"a": None, "b": None, "c": None}

    def test_two_siblings_overlap(self):
        sibs = [
            _step("a", start="2026-01-01 10:00:00", end="2026-01-01 10:05:00"),
            _step("b", start="2026-01-01 10:02:00", end="2026-01-01 10:07:00"),
        ]
        groups = detect_parallel_groups(sibs)
        assert groups["a"] is not None
        assert groups["a"] == groups["b"]   # same parallel group

    def test_three_siblings_two_overlap_third_sequential(self):
        sibs = [
            _step("a", start="2026-01-01 10:00:00", end="2026-01-01 10:05:00"),
            _step("b", start="2026-01-01 10:02:00", end="2026-01-01 10:07:00"),
            _step("c", start="2026-01-01 11:00:00", end="2026-01-01 11:05:00"),
        ]
        groups = detect_parallel_groups(sibs)
        assert groups["a"] == groups["b"]
        assert groups["a"] is not None
        assert groups["c"] is None  # sequential, alone

    def test_in_progress_step_overlaps_completed_sibling(self):
        """A step still IN_PROGRESS (no completed_at) but started while sibling was running → parallel."""
        sibs = [
            _step("a", start="2026-01-01 10:00:00", end="2026-01-01 10:05:00", status="COMPLETED"),
            _step("b", start="2026-01-01 10:02:00", end=None, status="IN_PROGRESS"),
        ]
        groups = detect_parallel_groups(sibs)
        assert groups["a"] == groups["b"]
        assert groups["a"] is not None

    def test_steps_without_started_at_are_alone(self):
        """PENDING steps with no started_at can't be parallel with anything."""
        sibs = [
            _step("a", start=None, end=None, status="PENDING"),
            _step("b", start="2026-01-01 10:00:00", end="2026-01-01 10:05:00"),
        ]
        groups = detect_parallel_groups(sibs)
        assert groups["a"] is None
        assert groups["b"] is None  # no peer to overlap with

    def test_exact_same_window_groups_them(self):
        """Steps started + completed at exactly the same timestamps (common with start-step racing) → parallel."""
        sibs = [
            _step("a", start="2026-01-01 10:00:00", end="2026-01-01 10:05:00"),
            _step("b", start="2026-01-01 10:00:00", end="2026-01-01 10:05:00"),
        ]
        groups = detect_parallel_groups(sibs)
        assert groups["a"] == groups["b"]


# ── build_step_tree ───────────────────────────────────────────────────────

class TestBuildStepTree:
    def test_flat_no_parents(self):
        steps = [
            _step("p-A", start="2026-01-01 10:00:00", end="2026-01-01 10:01:00", order=1),
            _step("p-B", start="2026-01-01 10:01:01", end="2026-01-01 10:02:00", order=2),
        ]
        tree = build_step_tree(steps)
        assert len(tree) == 2
        assert tree[0]["step_id"] == "p-A"
        assert tree[0]["children"] == []
        assert tree[0]["parallel_group_id"] is None  # alone at root level

    def test_nested_children_preserved(self):
        steps = [
            _step("p-A", order=1, start="2026-01-01 10:00:00", end="2026-01-01 10:01:00"),
            _step("p-A.1", parent="p-A", depth=1, order=2,
                  start="2026-01-01 10:00:30", end="2026-01-01 10:00:45"),
            _step("p-A.2", parent="p-A", depth=1, order=3,
                  start="2026-01-01 10:00:50", end="2026-01-01 10:00:55"),
        ]
        tree = build_step_tree(steps)
        assert len(tree) == 1
        assert tree[0]["step_id"] == "p-A"
        assert len(tree[0]["children"]) == 2
        kids = {c["step_id"] for c in tree[0]["children"]}
        assert kids == {"p-A.1", "p-A.2"}

    def test_parallel_siblings_at_root(self):
        """Realistic case from exec-deterministic plan: C + D started at same time."""
        steps = [
            _step("p-A", order=1, start="2026-01-01 10:00:00", end="2026-01-01 10:01:00"),
            _step("p-B", order=2, start="2026-01-01 10:01:01", end="2026-01-01 10:02:00"),
            _step("p-C", order=3, start="2026-01-01 10:02:01", end="2026-01-01 10:03:00"),
            _step("p-D", order=4, start="2026-01-01 10:02:01", end="2026-01-01 10:03:00"),
        ]
        tree = build_step_tree(steps)
        # 4 root steps; C + D should share parallel_group_id; A + B alone
        by_id = {s["step_id"]: s for s in tree}
        assert by_id["p-A"]["parallel_group_id"] is None
        assert by_id["p-B"]["parallel_group_id"] is None
        assert by_id["p-C"]["parallel_group_id"] is not None
        assert by_id["p-C"]["parallel_group_id"] == by_id["p-D"]["parallel_group_id"]

    def test_parallels_at_depth_1(self):
        """Sub-steps from a deviation that ran in parallel."""
        steps = [
            _step("p-A", order=1, start="2026-01-01 10:00:00", end="2026-01-01 10:05:00"),
            _step("p-A.1", parent="p-A", depth=1, order=2,
                  start="2026-01-01 10:01:00", end="2026-01-01 10:02:00"),
            _step("p-A.2", parent="p-A", depth=1, order=3,
                  start="2026-01-01 10:01:00", end="2026-01-01 10:02:00"),
        ]
        tree = build_step_tree(steps)
        kids = tree[0]["children"]
        assert kids[0]["parallel_group_id"] == kids[1]["parallel_group_id"]
        assert kids[0]["parallel_group_id"] is not None

    def test_orphan_with_missing_parent_treated_as_root(self):
        """Defensive: child whose parent isn't in the steps list shouldn't be lost."""
        steps = [
            _step("p-orphan", parent="missing-parent", depth=1, order=1,
                  start="2026-01-01 10:00:00", end="2026-01-01 10:01:00"),
        ]
        tree = build_step_tree(steps)
        assert len(tree) == 1
        assert tree[0]["step_id"] == "p-orphan"
