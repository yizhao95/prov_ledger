"""021 — trigger_log gains the verdict 'retract' (DP phase 2d, Task 0).

The file rebuilds the table to widen a CHECK, and the first draft of it silently
dropped 019's 'ambiguous' — three suites caught that within the same run. The
enum is pinned here so the next rebuild cannot lose a value either.
"""
import sqlite3

import pytest

VERDICTS = ("auto", "ask", "silent", "ambiguous", "retract")


@pytest.mark.parametrize("verdict", VERDICTS)
def test_every_verdict_the_rules_write_is_still_accepted(conn, verdict):
    conn.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, rule_id, verdict, basis) "
                 "VALUES ('demo', 'P1', 'nk_a', 'code', 'R0', ?, 'b')", (verdict,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM trigger_log WHERE verdict = ?", (verdict,)).fetchone()[0] == 1


def test_an_invented_verdict_is_still_refused(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO trigger_log (project, plan_id, path, verdict) VALUES ('demo', 'P1', 'code', 'maybe')")


def test_the_rebuilt_table_is_still_append_only(conn):
    conn.execute("INSERT INTO trigger_log (project, plan_id, path, rule_id, verdict, basis) "
                 "VALUES ('demo', 'P1', 'code', 'R0', 'ask', 'b')")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE trigger_log SET basis = 'rewritten'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM trigger_log")


def test_the_index_survived_the_rebuild(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'trigger_log'")}
    assert "idx_trigger_log_plan" in names
