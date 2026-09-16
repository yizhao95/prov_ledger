"""023 — declared_node: the table that holds the world outside the code.

Append-only like every other provenance table: one INSERT per version, the
only column an UPDATE may touch is `superseded_by`, DELETE is refused. `tier`
is a two-value enum — a declared node is `stated` or `asserted` and NEVER
`observed`, because nobody observed it: somebody said it.
"""
import sqlite3

import pytest

COLUMNS = {"id", "project", "slug", "qualified_name", "node_type", "description", "attrs_json",
           "links_json", "links_checked", "state", "tier", "field_tiers_json",
           "description_utterance_id", "version", "supersedes", "superseded_by", "recorded_by",
           "model", "occurred_at", "recorded_at", "prev_hash", "hash"}


def _cols(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(declared_node)")}


def _row(conn, **over):
    row = dict(project="demo", slug="emea-excluded", qualified_name="declared:emea-excluded",
               node_type="business_rule", description="EMEA excluded from the Q3 rollup",
               attrs_json="{}", links_json="[]", links_checked=1, state="active", tier="stated",
               field_tiers_json="{}", version=1, recorded_by="human",
               occurred_at="2026-03-14 09:00:00", hash="h1")
    row.update(over)
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO declared_node ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM declared_node").fetchone()[0]


def test_023_creates_declared_node_with_every_column_the_feature_needs(conn):
    assert _cols(conn) == COLUMNS


def test_a_declared_node_is_never_observed(conn):
    _row(conn, tier="asserted", slug="s2", qualified_name="declared:s2", hash="h2")
    with pytest.raises(sqlite3.IntegrityError):
        _row(conn, tier="observed", slug="s3", qualified_name="declared:s3", hash="h3")


def test_only_superseded_by_may_change_and_nothing_may_be_deleted(conn):
    rid = _row(conn)
    conn.execute("UPDATE declared_node SET superseded_by = 99 WHERE id = ?", (rid,))
    conn.commit()
    assert conn.execute("SELECT superseded_by FROM declared_node WHERE id = ?", (rid,)).fetchone()[0] == 99
    for col, value in (("description", "something else"), ("tier", "asserted"), ("attrs_json", '{"a":1}'),
                       ("state", "retired"), ("hash", "tampered")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE declared_node SET {col} = ? WHERE id = ?", (value, rid))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM declared_node WHERE id = ?", (rid,))


def test_the_node_type_enum_is_the_five_kinds_of_real_world_object(conn):
    for i, t in enumerate(("external_system", "business_rule", "stakeholder_decision",
                           "external_dataset", "manual_figure")):
        _row(conn, node_type=t, slug=f"s{i}", qualified_name=f"declared:s{i}", hash=f"h{i}")
    with pytest.raises(sqlite3.IntegrityError):
        _row(conn, node_type="function", slug="sx", qualified_name="declared:sx", hash="hx")
