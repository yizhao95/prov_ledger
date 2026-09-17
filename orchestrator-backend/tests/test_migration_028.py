"""028 — artifact_file / occurrence / anchor_state: the file is where a number
was seen, not what the number is (DP phase 4, spec §9).

The identity of a figure in a deck is its data source — `metric:q3_conv`,
`orders.net_revenue`, `declared:<slug>` — and the deck is one place it turned
up. So `occurrence` points at the node and merely names the file; renaming the
deck, or shipping a v2, changes nothing about the node's history.

Three invariants this migration carries:

* `occurrence` is append-only and hash-chained like utterance / reference /
  change_reason. A number that was seen in a file was seen; a later value is a
  new row.
* `occurrence.tier` can only be `observed`. Somebody read the figure out of the
  file — nobody inferred it. A manual figure is a declared node (026), not an
  occurrence, and the two must not be able to wear each other's clothes.
* `anchor_state` is append-only too: every check writes its verdict, so
  `anchor_lost` has a timestamp and a reason rather than being a computed
  opinion that the next check can quietly reverse.

`artifact_file` is the one table with a mutable column, and exactly one:
`last_seen`. The path, the sha256 and the kind are what the row IS.
"""
import sqlite3

import pytest

from orchestrator import provenance

ARTIFACT_FILE_COLUMNS = {"id", "project", "path", "sha256", "kind", "first_seen", "last_seen"}
OCCURRENCE_COLUMNS = {"id", "project", "node_key", "file_id", "locator_json", "value_text", "value_num",
                      "seen_at", "tier", "by", "recorded_at", "prev_hash", "hash"}
ANCHOR_STATE_COLUMNS = {"id", "occurrence_id", "state", "checked_at", "reason"}


def _cols(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _file(conn, **over) -> int:
    row = dict(project="demo", path="decks/q3.pptx", sha256="a" * 64, kind="pptx")
    row.update(over)
    cols = ", ".join(row)
    cur = conn.execute(f"INSERT INTO artifact_file ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    return int(cur.lastrowid)


def _occurrence(conn, file_id: int | None = None, **over) -> int:
    row = dict(project="demo", node_key="metric:q3_conv", file_id=file_id if file_id is not None else _file(conn),
               locator_json='{"kind": "pptx", "slide": 4, "shape": 2}', value_text="3.2", value_num=3.2,
               seen_at="2026-09-17 09:00:00", tier="observed", by="human",
               recorded_at="2026-09-17 09:00:00")
    row.update(over)
    rid = provenance._insert_chained(conn, "occurrence", row)
    conn.commit()
    return rid


def _state(conn, occurrence_id: int, **over) -> int:
    row = dict(occurrence_id=occurrence_id, state="ok", checked_at="2026-09-17 10:00:00", reason=None)
    row.update(over)
    cols = ", ".join(row)
    cur = conn.execute(f"INSERT INTO anchor_state ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    return int(cur.lastrowid)


# ── shape ────────────────────────────────────────────────────────────────────

def test_028_creates_the_three_tables_with_every_column_the_feature_needs(conn):
    assert _cols(conn, "artifact_file") == ARTIFACT_FILE_COLUMNS
    assert _cols(conn, "occurrence") == OCCURRENCE_COLUMNS
    assert _cols(conn, "anchor_state") == ANCHOR_STATE_COLUMNS


def test_an_occurrence_points_at_a_node_and_only_names_a_file(conn):
    """The node_key is the data source; the file is a foreign key to where it
    was seen. Two files may carry the same node, and that is the point."""
    a, b = _file(conn, path="decks/q3_v1.pptx"), _file(conn, path="decks/q3_v2.pptx", sha256="b" * 64)
    _occurrence(conn, a)
    _occurrence(conn, b, locator_json='{"kind": "pptx", "slide": 6, "shape": 2}')
    rows = conn.execute("SELECT node_key, file_id FROM occurrence ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["metric:q3_conv", "metric:q3_conv"]
    assert {r[1] for r in rows} == {a, b}


# ── append-only ──────────────────────────────────────────────────────────────

def test_an_occurrence_cannot_be_edited_or_deleted(conn):
    rid = _occurrence(conn)
    for col, value in (("node_key", "metric:other"), ("locator_json", '{"kind": "pptx", "slide": 6}'),
                       ("value_text", "9.9"), ("value_num", 9.9), ("tier", "asserted"), ("seen_at", "2020-01-01 00:00:00")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE occurrence SET {col} = ? WHERE id = ?", (value, rid))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM occurrence WHERE id = ?", (rid,))


def test_an_anchor_check_that_happened_cannot_be_rewritten(conn):
    """A verdict is a record of a moment. The next check appends a new one."""
    rid = _occurrence(conn)
    sid = _state(conn, rid)
    for col, value in (("state", "anchor_lost"), ("reason", "never mind"), ("checked_at", "2020-01-01 00:00:00")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE anchor_state SET {col} = ? WHERE id = ?", (value, sid))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM anchor_state WHERE id = ?", (sid,))
    _state(conn, rid, state="anchor_lost", reason="the value is no longer at that locator: moved or removed",
           checked_at="2026-09-17 11:00:00")
    assert [r[0] for r in conn.execute("SELECT state FROM anchor_state ORDER BY id")] == ["ok", "anchor_lost"]


def test_only_last_seen_may_change_on_an_artifact_file(conn):
    fid = _file(conn)
    conn.execute("UPDATE artifact_file SET last_seen = ? WHERE id = ?", ("2026-09-18 08:00:00", fid))
    conn.commit()
    assert conn.execute("SELECT last_seen FROM artifact_file WHERE id = ?", (fid,)).fetchone()[0] == "2026-09-18 08:00:00"
    for col, value in (("path", "decks/renamed.pptx"), ("sha256", "c" * 64), ("kind", "docx"),
                       ("project", "other"), ("first_seen", "2020-01-01 00:00:00")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE artifact_file SET {col} = ? WHERE id = ?", (value, fid))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM artifact_file WHERE id = ?", (fid,))


# ── the enums that keep the tiers apart ──────────────────────────────────────

def test_an_occurrence_is_observed_and_can_be_nothing_else(conn):
    """Somebody read this number out of this file. There is no reading of a
    file that produces an assertion, so the other tiers are refused here."""
    _occurrence(conn, tier="observed")
    for tier in ("asserted", "stated", "derived", "unstated"):
        with pytest.raises(sqlite3.IntegrityError):
            _occurrence(conn, tier=tier)


def test_an_occurrence_names_who_saw_it(conn):
    for by in ("human", "agent", "system"):
        _occurrence(conn, by=by)
    with pytest.raises(sqlite3.IntegrityError):
        _occurrence(conn, by="somebody")


def test_an_anchor_state_is_ok_or_anchor_lost_and_nothing_in_between(conn):
    rid = _occurrence(conn)
    _state(conn, rid, state="ok")
    _state(conn, rid, state="anchor_lost", reason="file is gone")
    for bad in ("moved", "unknown", "maybe", "relocated"):
        with pytest.raises(sqlite3.IntegrityError):
            _state(conn, rid, state=bad)


# ── the chain ────────────────────────────────────────────────────────────────

def test_occurrences_are_hash_chained_and_verify_chain_walks_them(conn):
    for i in range(3):
        _occurrence(conn, value_text=str(3.2 + i), value_num=3.2 + i)
    rows = [dict(r) for r in conn.execute("SELECT * FROM occurrence ORDER BY id")]
    assert rows[0]["prev_hash"] is None
    assert [r["prev_hash"] for r in rows[1:]] == [r["hash"] for r in rows[:-1]]
    assert provenance.verify_chain(conn, "occurrence") == {"ok": True, "rows": 3, "first_bad_id": None}


def test_a_row_written_around_the_store_is_named(conn):
    """The chain exists so that an INSERT that did not go through
    `_insert_chained` is visible — not merely improbable."""
    _occurrence(conn)
    conn.execute("INSERT INTO occurrence (project, node_key, file_id, locator_json, value_text, value_num, "
                 "seen_at, tier, by, recorded_at, prev_hash, hash) "
                 "VALUES ('demo', 'metric:q3_conv', 1, '{}', '9.9', 9.9, '2026-09-17 09:00:00', "
                 "'observed', 'agent', '2026-09-17 09:00:00', 'nonsense', 'nonsense')")
    conn.commit()
    out = provenance.verify_chain(conn, "occurrence")
    assert out["ok"] is False and out["first_bad_id"] == 2


# ── idempotence ──────────────────────────────────────────────────────────────

def test_running_the_migration_twice_changes_nothing(conn):
    from orchestrator import db
    rid = _occurrence(conn)
    db.run_migrations(conn)
    assert _cols(conn, "occurrence") == OCCURRENCE_COLUMNS
    assert conn.execute("SELECT COUNT(*) FROM occurrence").fetchone()[0] == 1
    assert conn.execute("SELECT id FROM occurrence").fetchone()[0] == rid
