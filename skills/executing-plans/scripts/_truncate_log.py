"""Head+tail log truncation for run-step.sh.

Pure function — no DB, no I/O. Used by `run-step.sh` to keep
`Steps.log_context` rows bounded (default cap 16384 bytes) so chatty
build/test output doesn't bloat the SQLite database.

The marker text reports exact bytes removed so anyone reading a truncated
log can quickly see what slice was discarded:

    <first 8 KiB>
    --- TRUNCATED 12345 BYTES ---
    <last 8 KiB>
"""
from __future__ import annotations

DEFAULT_CAP = 16384
MARKER_FMT = "\n--- TRUNCATED {n} BYTES ---\n"


def head_tail_truncate(text: str, cap: int = DEFAULT_CAP) -> str:
    """Return `text` unchanged if ≤ cap; otherwise head + marker + tail.

    The marker is NOT counted against the cap (it's metadata, not payload).
    Output size when truncated: cap + len(marker_with_real_count).

    Args:
        text: The raw log content.
        cap: Total bytes of payload to preserve (split half-and-half).
             Must be even (so head and tail are equal size).
    """
    if len(text) <= cap:
        return text
    half = cap // 2
    head = text[:half]
    tail = text[-half:]
    removed = len(text) - cap
    marker = MARKER_FMT.format(n=removed)
    return head + marker + tail


if __name__ == "__main__":  # pragma: no cover — manual smoke
    import sys
    content = sys.stdin.read()
    sys.stdout.write(head_tail_truncate(content))
