"""Telemetry pipeline — log truncation + (heuristic) summarization.

Phase 4-MVP uses heuristic head+tail summary. Future enhancement: cheap-LLM
summarization via Element/Pydantic AI.
"""
from __future__ import annotations

import sqlite3

MAX_LINES = 50
MAX_CHARS_BEFORE_SUMMARY = 4000  # ≈ 1000 tokens at ~4 chars/token


def truncate_for_log(raw: str) -> str:
    """Apply Phase 1 telemetry rules: tail-50, then head+tail summarize if still huge."""
    if not raw:
        return ""
    lines = raw.splitlines()
    if len(lines) > MAX_LINES:
        lines = lines[-MAX_LINES:]
    out = "\n".join(lines)
    if len(out) > MAX_CHARS_BEFORE_SUMMARY:
        header = (
            f"[summarized — original was {len(raw)} chars / "
            f"{len(raw.splitlines())} lines]\n"
        )
        # Head + tail with a middle elision. BE-C6: make head and tail
        # NON-OVERLAPPING and clamp the elided count to >= 0. The old code used
        # lines[:5] + lines[-5:], which for <=10 lines overlapped (duplicating
        # content) and computed a negative "[-N lines elided]". Starting the tail
        # at max(5, len-5) guarantees no overlap and a non-negative count.
        head = lines[:5]
        tail_start = max(5, len(lines) - 5)
        elided = tail_start - 5
        middle = f"... [{elided} lines elided] ...\n" if elided else ""
        out = header + "\n".join(head) + "\n" + middle + "\n".join(lines[tail_start:])
    return out


def append_step_log(
    conn: sqlite3.Connection,
    step_id: str,
    raw_chunk: str,
    separator: str = "\n---\n",
) -> str:
    """Append a chunk of telemetry to the step's log_context. Returns the new log_context."""
    cur = conn.execute("SELECT log_context FROM Steps WHERE step_id = ?", (step_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"step_id not found: {step_id}")
    existing = row["log_context"] if row["log_context"] else ""
    truncated_chunk = truncate_for_log(raw_chunk)
    combined = (existing + separator if existing else "") + truncated_chunk
    # Re-truncate combined log to keep it bounded
    final = truncate_for_log(combined)
    conn.execute("UPDATE Steps SET log_context = ? WHERE step_id = ?", (final, step_id))
    conn.commit()
    return final
