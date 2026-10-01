"""leakcheck — did anything this run created reach the developer's ledger?

The question has to be answered structurally, not textually. A first attempt
searched the ledger's raw bytes for the sandbox path and reported a leak: the
path really was in there, written by the provLedger hooks of the Claude Code
session that *launched the check* — the session's own words, not the check's
rows. A needle that the session can type is not a needle.

So this asks the only thing that actually matters: are there rows belonging to
a project this run invented? Every project name the check creates begins with
the dummy prefix and carries the run's nonce, and nothing else in the world
writes one. The ledger is opened read-only, the way the dashboard opens it, so
asking cannot itself change the answer.

Usage: leakcheck.py <ledger.db> <project-prefix>
Prints one line per table that holds such a row, and nothing at all when clean.
"""
from __future__ import annotations

import sqlite3
import sys

# every table that carries a project column and could hold a row of ours
TABLES = ("Plans", "change_reason", "utterance", "reference", "LedgerEntries",
          "expectations", "metrics", "declared_node", "ask_log")


def leaks(db: str, prefix: str) -> list[str]:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return [f"could not open {db} read-only to check: {e}"]
    out = []
    try:
        for table in TABLES:
            try:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project LIKE ?", (prefix + "%",)
                ).fetchone()[0]
            except sqlite3.Error:
                continue                      # the table or the column is not there
            if n:
                out.append(f"{n} row(s) in {table} belong to a project named {prefix}*")
    finally:
        conn.close()
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: leakcheck.py <ledger.db> <project-prefix>", file=sys.stderr)
        return 64
    for line in leaks(argv[1], argv[2]):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
