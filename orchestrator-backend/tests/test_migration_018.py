"""Migration 018 — decision provenance: utterance / reference / change_reason /
reference_link / trigger_log, the tier rules as CHECKs, append-only triggers
with an UPDATE whitelist, and the computed source level (evidence_level)."""
import sqlite3

import pytest

from orchestrator import db

TABLES = {"tool_call_log", "utterance", "reference", "change_reason", "reference_link", "trigger_log", "migration_state"}
VIEWS = {"change_reason_v", "node_reason_v"}


def _names(conn, kind):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def _utt(conn, text="please keep fiscal weeks", **kw):
    row = {"session_id": "s", "project": "p", "plan_id": "P1", "text": text, "occurred_at": "2026-09-15 10:00:00", "hash": "h"}
    row.update(kw)
    cols = ", ".join(row); ph = ", ".join("?" * len(row))
    return conn.execute(f"INSERT INTO utterance ({cols}) VALUES ({ph})", tuple(row.values())).lastrowid


def _ref(conn, **kw):
    row = {"project": "p", "kind": "email", "uri": "mail:1", "label": "the email", "occurred_at": "2026-09-15 09:00:00",
           "verifiability": "linked", "hash": "h"}
    row.update(kw)
    cols = ", ".join(row); ph = ", ".join("?" * len(row))
    return conn.execute(f"INSERT INTO reference ({cols}) VALUES ({ph})", tuple(row.values())).lastrowid


def _reason(conn, **kw):
    row = {"project": "p", "node_key": "nk_a", "plan_id": "P1", "kind": "technical", "occurred_at": "2026-09-15 10:00:00",
           "recorded_by": "agent", "tier": "asserted", "interpretation": "weekly grain", "hash": "h"}
    row.update(kw)
    row = {k: v for k, v in row.items() if v is not ...}
    cols = ", ".join(row); ph = ", ".join("?" * len(row))
    return conn.execute(f"INSERT INTO change_reason ({cols}) VALUES ({ph})", tuple(row.values())).lastrowid


def test_018_tables_views_and_triggers_exist(conn):
    assert TABLES <= _names(conn, "table") and VIEWS <= _names(conn, "view")
    trig = _names(conn, "trigger")
    for t in ("utterance", "reference", "change_reason", "reference_link", "trigger_log", "tool_call_log"):
        assert f"trg_{t}_no_delete" in trig, t
    assert {"trg_utterance_no_update", "trg_reference_update_whitelist", "trg_change_reason_update_whitelist"} <= trig
    assert db.run_migrations(conn) == 0                       # idempotent on a migrated DB
    assert "018_decision_provenance.sql" in {r[0] for r in conn.execute("SELECT migration_file FROM schema_version")}


@pytest.mark.parametrize("bad, match", [
    ({"tier": "stated", "interpretation": None}, "CHECK"),                                   # stated without a verbatim pointer
    ({"tier": "asserted", "interpretation": None, "statement": None}, "CHECK"),              # asserted without a text
    ({"tier": "derived", "interpretation": "r", "rule_id": None}, "CHECK"),                  # derived without a rule id
    ({"tier": "unstated", "interpretation": "something"}, "CHECK"),                           # unstated carrying text
    ({"node_key": None, "role": "reason"}, "CHECK"),                                          # a reason without an anchor
    ({"kind": "nope"}, "CHECK"),
    ({"recorded_by": "model"}, "CHECK"),
])
def test_change_reason_checks_refuse_each_counterexample(conn, bad, match):
    with pytest.raises(sqlite3.IntegrityError, match=match):
        _reason(conn, **bad)


def test_stated_requires_a_span_inside_the_utterance_pointer(conn):
    u = _utt(conn)
    _reason(conn, tier="stated", interpretation=None, verbatim_utterance_id=u, verbatim_start=0, verbatim_end=6)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _reason(conn, tier="stated", interpretation=None, verbatim_utterance_id=u)                      # no span
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _reason(conn, tier="stated", interpretation=None, verbatim_utterance_id=u, verbatim_start=5, verbatim_end=5)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _reason(conn, tier="asserted", verbatim_utterance_id=u, verbatim_start=0, verbatim_end=6)       # pointer but not stated


@pytest.mark.parametrize("bad", [
    {"kind": "verbal", "uri": "x", "verifiability": "verbal"},          # a verbal reference has no uri
    {"kind": "verbal", "uri": None, "verifiability": "linked"},         # verbal must say verbal
    {"kind": "email", "uri": None, "verifiability": "linked"},          # linked needs a uri
    {"kind": "email", "uri": None, "verifiability": "verbal"},          # only verbal is verbal
    {"label": "x" * 513},                                               # labels are pointers, not bodies (B4)
    {"kind": "sms"},
])
def test_reference_checks(conn, bad):
    with pytest.raises(sqlite3.IntegrityError):
        _ref(conn, **bad)
    _ref(conn, kind="verbal", uri=None, verifiability="verbal")
    _ref(conn, kind="email", uri=None, verifiability="unreachable")


def test_update_whitelist_superseded_by_and_last_checked_only(conn):
    r1 = _reason(conn); r2 = _reason(conn)
    conn.execute("UPDATE change_reason SET superseded_by=? WHERE id=?", (r2, r1))
    assert conn.execute("SELECT superseded_by FROM change_reason WHERE id=?", (r1,)).fetchone()[0] == r2
    for col, val in (("interpretation", "rewritten"), ("tier", "stated"), ("state", "expired"), ("hash", "z"), ("occurred_at", "2020-01-01")):
        with pytest.raises(sqlite3.IntegrityError, match="only superseded_by"):
            conn.execute(f"UPDATE change_reason SET {col}=? WHERE id=?", (val, r1))
    f = _ref(conn)
    conn.execute("UPDATE reference SET last_checked='2026-09-15 12:00:00' WHERE id=?", (f,))
    with pytest.raises(sqlite3.IntegrityError, match="only last_checked"):
        conn.execute("UPDATE reference SET label='renamed' WHERE id=?", (f,))
    u = _utt(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE utterance SET text='rewritten' WHERE id=?", (u,))


@pytest.mark.veto
def test_g2_delete_is_refused_on_every_provenance_table(conn):
    u = _utt(conn); f = _ref(conn); r = _reason(conn)
    conn.execute("INSERT INTO reference_link (reason_id, reference_id) VALUES (?, ?)", (r, f))
    conn.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, rule_id, verdict) VALUES ('p', 'P1', 'nk_a', 'code', 'R1', 'auto')")
    conn.execute("INSERT INTO tool_call_log (session_id, tool_name) VALUES ('s', 'Bash')")
    for table in ("utterance", "reference", "change_reason", "reference_link", "trigger_log", "tool_call_log"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM {table}")
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1, table


@pytest.mark.veto
def test_i5_evidence_level_is_computed_never_stored(conn):
    assert "evidence_level" not in {r[1] for r in conn.execute("PRAGMA table_info(change_reason)")}
    assert "evidence_level" in {r[1] for r in conn.execute("PRAGMA table_info(change_reason_v)")}
    u = _utt(conn)
    linked = _ref(conn); verbal = _ref(conn, kind="verbal", uri=None, verifiability="verbal")
    a = _reason(conn)                                                                       # only the plan anchors it
    conn.execute("INSERT INTO reference_link VALUES (?, ?)", (a, linked))
    b = _reason(conn, tier="stated", interpretation=None, verbatim_utterance_id=u, verbatim_start=0, verbatim_end=6)
    c = _reason(conn)
    conn.execute("INSERT INTO reference_link VALUES (?, ?)", (c, verbal))
    d = _reason(conn)
    e = _reason(conn, tier="unstated", interpretation=None, recorded_by="system")
    levels = dict(conn.execute("SELECT id, evidence_level FROM change_reason_v").fetchall())
    assert levels == {a: "linked", b: "verbal", c: "verbal", d: "task_context", e: "unstated"}


def test_i6_every_reason_is_at_least_task_context_unless_unstated(conn):
    _reason(conn); _reason(conn, tier="derived", rule_id="R4", interpretation="pure refactor", recorded_by="system")
    assert {r[0] for r in conn.execute("SELECT evidence_level FROM change_reason_v")} == {"task_context"}


def test_node_reason_v_has_exactly_the_old_columns(conn):
    old = [r[1] for r in conn.execute("PRAGMA table_info(node_reason)")]
    new = [r[1] for r in conn.execute("PRAGMA table_info(node_reason_v)")]
    assert set(old) == set(new)
    u = _utt(conn)
    _reason(conn, tier="stated", interpretation=None, verbatim_utterance_id=u, verbatim_start=0, verbatim_end=6)
    _reason(conn, role="constraint", kind="organizational", statement="keep paid orders", interpretation=None, recorded_by="human")
    _reason(conn, tier="unstated", interpretation=None, recorded_by="system")
    rows = [tuple(r) for r in conn.execute("SELECT kind, text, source, tier FROM node_reason_v ORDER BY id")]
    assert rows == [("reason", None, "agent", "stated"), ("constraint_ref", "keep paid orders", "human", "asserted"),
                    ("reason", None, "system", "derived")]


def test_reconcile_on_a_db_that_applied_018_without_bookkeeping(tmp_path):
    """The FL-021 pattern: a DB that ran every file but recorded 018 without its
    filename must reconcile and then apply nothing new."""
    c = db.open_db(tmp_path / "legacy.db")
    files = sorted(db.MIGRATIONS_DIR.glob("*.sql"))
    for f in files:
        c.executescript(f.read_text())
        c.execute("INSERT OR IGNORE INTO schema_version (version) VALUES ((SELECT COALESCE(MAX(version), 0) + 1 FROM schema_version))")
    c.commit()
    assert db.run_migrations(c) == 0
    assert "018_decision_provenance.sql" in {r[0] for r in c.execute("SELECT migration_file FROM schema_version")}
    assert TABLES <= _names(c, "table")
    c.close()


def test_recorded_at_is_written_by_the_db(conn):
    r = _reason(conn)
    at = conn.execute("SELECT recorded_at FROM change_reason WHERE id=?", (r,)).fetchone()[0]
    assert len(at) == 19 and at[4] == "-" and at[13] == ":"
