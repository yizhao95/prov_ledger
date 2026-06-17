"""Unit tests for _truncate_log.head_tail_truncate.

Pure function: head + tail truncation with a clear marker.
No DB, no shell, no orchestrator involvement — fast feedback for the helper
that the run-step.sh wrapper uses to keep log_context inside its budget.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from _truncate_log import head_tail_truncate, MARKER_FMT  # noqa: E402


CAP = 16384       # 16 KiB total budget
HALF = CAP // 2   # 8192 bytes head + 8192 bytes tail


def test_under_cap_passthrough():
    """Content under the cap is returned unchanged (no marker injected)."""
    payload = "hello world\n" * 100   # ~1.2 KB, well under cap
    out = head_tail_truncate(payload, cap=CAP)
    assert out == payload, "small payload must round-trip exactly"
    assert "TRUNCATED" not in out, "no marker should be injected when under cap"


def test_over_cap_head_plus_tail():
    """Content over the cap is split: head bytes + marker + tail bytes."""
    # 32 KB of distinct head/tail content so we can verify both ends survive
    head_block = "H" * HALF
    middle_block = "M" * HALF
    tail_block = "T" * HALF
    payload = head_block + middle_block + tail_block   # 24576 bytes total > 16384

    out = head_tail_truncate(payload, cap=CAP)

    # Head preserved (first HALF chars all 'H')
    assert out.startswith("H" * HALF), "first 8 KiB must be the original head"
    # Tail preserved (last HALF chars all 'T')
    assert out.endswith("T" * HALF), "last 8 KiB must be the original tail"
    # Middle must include the truncation marker
    assert "TRUNCATED" in out, "marker required when truncation occurred"
    # Middle 'M's must be gone (proof we cut, not just appended)
    assert "M" * HALF not in out, "the middle block must NOT survive"


def test_marker_format_and_byte_count():
    """Marker must declare exactly how many bytes were removed."""
    # Construct payload where we know the exact truncated byte count
    head_block = "H" * HALF      # 8192
    cut_block = "X" * 5000       # exactly 5000 bytes that should be cut
    tail_block = "T" * HALF      # 8192
    payload = head_block + cut_block + tail_block   # 21384 bytes

    out = head_tail_truncate(payload, cap=CAP)

    expected_marker = MARKER_FMT.format(n=5000)
    assert expected_marker in out, f"expected marker {expected_marker!r} in output"
    # Total output size: HALF + marker_len + HALF (marker excluded from budget per spec)
    expected_size = HALF + len(expected_marker) + HALF
    assert len(out) == expected_size, (
        f"output should be exactly {expected_size} bytes, got {len(out)}"
    )
