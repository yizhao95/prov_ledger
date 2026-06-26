"""Tests for telemetry.py — log truncation + append."""
import pytest

from orchestrator import db, telemetry
from orchestrator.telemetry import MAX_CHARS_BEFORE_SUMMARY, MAX_LINES, truncate_for_log


def test_truncate_short_unchanged():
    s = "hello\nworld"
    assert truncate_for_log(s) == s


def test_truncate_empty():
    assert truncate_for_log("") == ""


def test_truncate_few_but_long_lines_no_negative_elided():
    # BE-C6: 6 very long lines exceed MAX_CHARS but are <=10 lines; the old
    # head+tail path produced "[-4 lines elided]" and duplicated content.
    raw = "\n".join(["x" * 2000 for _ in range(6)])
    out = truncate_for_log(raw)
    assert "elided" not in out          # no head/tail summary for <=10 lines
    assert "[truncated]" in out         # hard char-truncation path used
    assert len(out) < len(raw)          # actually truncated


def test_truncate_at_max_lines_unchanged():
    s = "\n".join(f"line {i}" for i in range(MAX_LINES))
    assert truncate_for_log(s) == s


def test_truncate_above_max_lines_keeps_tail():
    s = "\n".join(f"line {i}" for i in range(MAX_LINES + 20))
    out = truncate_for_log(s)
    out_lines = out.splitlines()
    assert len(out_lines) <= MAX_LINES
    # last line should still be the actual last line
    assert out_lines[-1] == f"line {MAX_LINES + 19}"


def test_truncate_huge_summarized_with_head_tail():
    # Build a string that's > MAX_CHARS_BEFORE_SUMMARY but ≤ MAX_LINES lines (long lines)
    long_line = "x" * (MAX_CHARS_BEFORE_SUMMARY // 10)
    s = "\n".join([long_line] * 30)  # 30 lines, total ~12k chars
    out = truncate_for_log(s)
    assert "summarized" in out
    assert "lines elided" in out


def test_append_step_log_writes_to_db(conn):
    db.insert_plan(conn, "p1", "g")
    db.insert_step(conn, "p1-A", "p1", "step", execution_order=0)
    final = telemetry.append_step_log(conn, "p1-A", "first chunk")
    assert "first chunk" in final
    final2 = telemetry.append_step_log(conn, "p1-A", "second chunk")
    assert "first chunk" in final2 and "second chunk" in final2


def test_append_step_log_missing_step_raises(conn):
    with pytest.raises(ValueError, match="not found"):
        telemetry.append_step_log(conn, "ghost", "x")


def test_append_step_log_truncates_combined(conn):
    """Repeated appends should not let log_context grow unbounded."""
    db.insert_plan(conn, "p1", "g")
    db.insert_step(conn, "p1-A", "p1", "step", execution_order=0)
    big = "y" * 5000
    for _ in range(5):
        telemetry.append_step_log(conn, "p1-A", big)
    final = db.get_step(conn, "p1-A")["log_context"]
    # Combined should still be bounded (summary applied)
    assert "summarized" in final or len(final) <= MAX_CHARS_BEFORE_SUMMARY * 2
