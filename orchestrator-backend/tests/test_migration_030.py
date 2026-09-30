"""Migration 030 — evidence: where a claim came from, and what backs it.

Four parts, and two of them rebuild a table other code already reads:

  utterance.origin   an ADD COLUMN. Which door the words came through — the
                     hook that captured your keystrokes, a person at a
                     terminal, an agent running the CLI. `recorded_by` already
                     says who, but the writer declares that itself; origin is
                     decided by the entry point, so it is the checkable half.
  reference_check    a new append-only table: someone opened the pointer on
                     this date and it was there, or it was not.
  reference_link     rebuilt — gains utterance_id (a pointer can be pinned the
                     moment the words are said, before any reason exists) and
                     stance (evidence can contradict; discarding it silently is
                     the thing this project refuses to do).
  change_reason_v    rebuilt from 020's body plus utterance_origin. NOT from
                     018's: 020 replaced it and added significance_eff, and 022
                     built node_badge_v on top. Starting from 018 would drop
                     significance_eff and break the badge.

The upgrade path is what most of this file tests, because a fresh database
proves nothing about a database that already has rows in it. `_at_029` builds
one by pointing the runner at a directory holding every migration but the last.
"""
import sqlite3
from pathlib import Path

import pytest

from orchestrator import db

MIGRATIONS = Path(db.__file__).resolve().parent / "migrations"
LAST = "030_evidence.sql"


@pytest.fixture
def at_029(tmp_path, monkeypatch):
    """A database migrated to 029 and carrying rows, the way a real one would be.

    Everything before 030 is applied through the real runner against a directory
    of symlinks, so the fixture cannot drift from the real migration order.
    """
    staged = tmp_path / "migrations_029"
    staged.mkdir()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name >= LAST:
            continue
        (staged / f.name).symlink_to(f)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.open_db(tmp_path / "old.db")
    db.run_migrations(conn)
    monkeypatch.undo()
    yield conn
    conn.close()


def _utt(conn, text="please keep fiscal weeks", **kw):
    row = {"session_id": "s", "project": "p", "plan_id": "P1", "text": text,
           "occurred_at": "2026-09-15 10:00:00", "hash": "h"}
    row.update(kw)
    cols, ph = ", ".join(row), ", ".join("?" * len(row))
    return conn.execute(f"INSERT INTO utterance ({cols}) VALUES ({ph})", tuple(row.values())).lastrowid


def _ref(conn, **kw):
    row = {"project": "p", "kind": "email", "uri": "mail:1", "label": "the email",
           "occurred_at": "2026-09-15 09:00:00", "verifiability": "linked", "hash": "h"}
    row.update(kw)
    row = {k: v for k, v in row.items() if v is not None or k in ("uri",)}
    cols, ph = ", ".join(row), ", ".join("?" * len(row))
    return conn.execute(f"INSERT INTO reference ({cols}) VALUES ({ph})", tuple(row.values())).lastrowid


def _reason(conn, **kw):
    row = {"project": "p", "node_key": "nk_a", "plan_id": "P1", "kind": "technical",
           "occurred_at": "2026-09-15 10:00:00", "recorded_by": "agent", "tier": "asserted",
           "interpretation": "weekly grain", "hash": "h"}
    row.update(kw)
    row = {k: v for k, v in row.items() if v is not ...}
    cols, ph = ", ".join(row), ", ".join("?" * len(row))
    return conn.execute(f"INSERT INTO change_reason ({cols}) VALUES ({ph})", tuple(row.values())).lastrowid


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# ── (a) the upgrade does not rewrite history ────────────────────────────────

def test_existing_utterances_come_through_the_upgrade_as_unknown(at_029):
    """A row written before origin existed cannot claim an origin it never had.

    It also cannot be backfilled: the append-only trigger refuses the UPDATE, so
    'we do not guess where old words came from' is enforced by the table rather
    than by discipline.
    """
    u = _utt(at_029, text="the words from before")
    before = at_029.execute("SELECT text, hash, recorded_at FROM utterance WHERE id = ?", (u,)).fetchone()

    # Everything from 030 onward, counted from the directory rather than
    # hardcoded: a later migration must not make this assertion wrong, and
    # an exact count still catches a migration that silently does not run.
    remaining = len([p for p in sorted(MIGRATIONS.glob("*.sql")) if p.name >= LAST])
    assert db.run_migrations(at_029) == remaining, f"every migration from {LAST} onward should apply"

    after = at_029.execute("SELECT text, hash, recorded_at, origin FROM utterance WHERE id = ?", (u,)).fetchone()
    assert after["origin"] == "unknown"
    assert (after["text"], after["hash"], after["recorded_at"]) == tuple(before)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        at_029.execute("UPDATE utterance SET origin = 'hook' WHERE id = ?", (u,))


# ── (b) the new column refuses anything outside the five doors ──────────────

@pytest.mark.parametrize("origin", ["hook", "human_cli", "agent_cli", "import", "unknown"])
def test_every_named_origin_is_accepted(conn, origin):
    assert "origin" in _cols(conn, "utterance")
    assert _utt(conn, origin=origin)


def test_an_origin_outside_the_enum_is_refused(conn):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _utt(conn, origin="trust_me")


# ── (c) reference_check is append-only, like everything else here ───────────

def test_a_check_can_be_recorded_and_then_never_altered(conn):
    r = _ref(conn)
    cid = conn.execute(
        "INSERT INTO reference_check (reference_id, checked_at, verdict, hash) VALUES (?, ?, ?, ?)",
        (r, "2026-09-26 10:00:00", "gone", "h1")).lastrowid
    assert conn.execute("SELECT verdict FROM reference_check WHERE id = ?", (cid,)).fetchone()[0] == "gone"

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE reference_check SET verdict = 'ok' WHERE id = ?", (cid,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM reference_check WHERE id = ?", (cid,))


def test_a_verdict_outside_the_four_is_refused(conn):
    r = _ref(conn)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO reference_check (reference_id, checked_at, verdict, hash) VALUES (?, ?, ?, ?)",
                     (r, "2026-09-26 10:00:00", "probably fine", "h"))


# ── (d) a pointer hangs on exactly one thing ────────────────────────────────

def test_a_pointer_attaches_to_a_reason_or_to_the_words_but_never_both(conn):
    """Before a plan exists there is no reason to hang a pointer on, only the
    sentence that named the source. After it exists, there is. Never both at
    once, or the same evidence would be counted twice."""
    r1, r2, r3 = _ref(conn), _ref(conn), _ref(conn)
    reason, utt = _reason(conn), _utt(conn)

    conn.execute("INSERT INTO reference_link (reason_id, reference_id) VALUES (?, ?)", (reason, r1))
    conn.execute("INSERT INTO reference_link (utterance_id, reference_id) VALUES (?, ?)", (utt, r2))

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO reference_link (reason_id, utterance_id, reference_id) VALUES (?, ?, ?)",
                     (reason, utt, r3))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO reference_link (reference_id) VALUES (?)", (r3,))


# ── (e) evidence is allowed to disagree ─────────────────────────────────────

def test_stance_defaults_to_supports_and_names_only_three_positions(conn):
    """Evidence sits where a test sits, and a test is allowed to fail. An email
    that contradicts the reason is recorded as contradicting it, not dropped."""
    reason, r1, r2 = _reason(conn), _ref(conn), _ref(conn)
    conn.execute("INSERT INTO reference_link (reason_id, reference_id) VALUES (?, ?)", (reason, r1))
    assert conn.execute("SELECT stance FROM reference_link WHERE reference_id = ?", (r1,)).fetchone()[0] == "supports"

    conn.execute("INSERT INTO reference_link (reason_id, reference_id, stance) VALUES (?, ?, 'contradicts')",
                 (reason, r2))
    stances = {r[0] for r in conn.execute("SELECT stance FROM reference_link WHERE reason_id = ?", (reason,))}
    assert stances == {"supports", "contradicts"}

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO reference_link (reason_id, reference_id, stance) VALUES (?, ?, 'maybe')",
                     (reason, _ref(conn)))


# ── (f) the rebuild keeps every row it was given ────────────────────────────

def test_the_rebuild_carries_existing_links_across_unchanged(at_029):
    """The riskiest part of this migration. Reason 1411 on this repository
    records what happens when a table moves and its readers do not: twelve
    golden lines flipped tier. Count before, count after, and check the pairs."""
    reason, r1, r2 = _reason(at_029), _ref(at_029), _ref(at_029)
    at_029.execute("INSERT INTO reference_link (reason_id, reference_id) VALUES (?, ?)", (reason, r1))
    at_029.execute("INSERT INTO reference_link (reason_id, reference_id) VALUES (?, ?)", (reason, r2))
    before = {tuple(r) for r in at_029.execute("SELECT reason_id, reference_id FROM reference_link")}

    db.run_migrations(at_029)

    after = {tuple(r) for r in at_029.execute("SELECT reason_id, reference_id FROM reference_link")}
    assert after == before, "a pointer that was attached before the rebuild is still attached"
    assert all(r[0] == "supports" for r in at_029.execute("SELECT stance FROM reference_link"))
    assert all(r[0] is None for r in at_029.execute("SELECT utterance_id FROM reference_link"))

    trig = {r[0] for r in at_029.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "trg_reference_link_no_delete" in trig, "the rebuild must put the append-only triggers back"


# ── (g) the view says which door the words came through ─────────────────────

def test_the_view_reports_the_origin_for_stated_rows_and_nothing_for_the_rest(conn):
    """A reason is `stated` because it points at recorded words. The view now
    also says how those words were recorded, so a sentence the hook captured
    and a sentence an agent typed into the CLI stop looking identical."""
    u_hook = _utt(conn, text="keep fiscal weeks", origin="hook")
    u_cli = _utt(conn, text="keep fiscal weeks", origin="agent_cli")
    stated_hook = _reason(conn, tier="stated", interpretation=None,
                          verbatim_utterance_id=u_hook, verbatim_start=0, verbatim_end=6)
    stated_cli = _reason(conn, tier="stated", interpretation=None,
                         verbatim_utterance_id=u_cli, verbatim_start=0, verbatim_end=6)
    asserted = _reason(conn)

    got = dict(conn.execute(
        "SELECT id, utterance_origin FROM change_reason_v WHERE id IN (?, ?, ?)",
        (stated_hook, stated_cli, asserted)).fetchall())
    assert got[stated_hook] == "hook"
    assert got[stated_cli] == "agent_cli"
    assert got[asserted] is None


# ── (h) nothing that already worked stops working ───────────────────────────

def test_the_four_source_levels_are_exactly_what_018_and_020_computed(conn):
    """evidence_level is the product's own claim about how checkable a record
    is. 030 rebuilds the view it lives in, so pin all four values."""
    u = _utt(conn)
    linked = _reason(conn)
    conn.execute("INSERT INTO reference_link (reason_id, reference_id) VALUES (?, ?)",
                 (linked, _ref(conn, verifiability="linked")))
    verbal = _reason(conn, tier="stated", interpretation=None,
                     verbatim_utterance_id=u, verbatim_start=0, verbatim_end=6)
    task_context = _reason(conn)
    unstated = _reason(conn, tier="unstated", interpretation=None)

    levels = dict(conn.execute(
        "SELECT id, evidence_level FROM change_reason_v WHERE id IN (?, ?, ?, ?)",
        (linked, verbal, task_context, unstated)).fetchall())
    assert levels[linked] == "linked"
    assert levels[verbal] == "verbal"
    assert levels[task_context] == "task_context"
    assert levels[unstated] == "unstated"


def test_significance_eff_survives_the_view_rebuild(conn):
    """020 added significance_eff and 022 built node_badge_v on it. Rebuilding
    change_reason_v from 018's body instead of 020's would drop the column and
    take the badge with it."""
    assert "significance_eff" in _cols(conn, "change_reason_v")
    assert "node_badge_v" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    r = _reason(conn)
    conn.execute("INSERT INTO significance_log (project, reason_id, hint, hint_basis, judged_by, at) "
                 "VALUES ('p', ?, 'minor', 'one-line change', 'hint', '2026-09-15 10:00:00')", (r,))
    assert conn.execute("SELECT significance_eff FROM change_reason_v WHERE id = ?", (r,)).fetchone()[0] == "minor"


def test_030_is_idempotent_and_recorded(conn):
    assert db.run_migrations(conn) == 0
    assert LAST in {r[0] for r in conn.execute("SELECT migration_file FROM schema_version")}
