"""The hash chain across migration 030, where `utterance` gained `origin`.

Adding a column to a chained table changes what `canonical()` hashes, so every
row written before the migration stops verifying. On the dogfood ledger that was
126 rows and `verify` said `chain utterance: chain broken at #1`. No suite caught
it, because every suite starts from a fresh database — which is the whole reason
the real ledger is worth running `verify` against.

Three ways out were available and two of them are wrong:

  backfill + re-hash   refused by trg_utterance_no_update, and rightly so
  origin in _UNCHAINED  the one column whose entire purpose is checkability
                        would then be the one column nothing checks
  accept the older form for a row that reads 'unknown'   <- this

The security property that makes the third one safe, and the thing these tests
exist to hold in place: the fallback is available **only** to a row claiming
`unknown`, which asserts nothing about where the words came from. Claiming
`hook` — the one value that means "captured from your keystrokes" — requires the
current canonical form, and therefore requires re-hashing every row after it.
A forger can downgrade a record into worthlessness; a forger cannot upgrade one.
"""
import hashlib
import json

import pytest

from orchestrator import provenance as pv

PRE030_COLS = ("session_id", "project", "plan_id", "text", "occurred_at", "recorded_at", "visibility")


def _pre030_hash(row: dict, prev: str | None) -> str:
    """The canonical form as it stood before 030 — the same JSON shape, minus a
    column that did not exist yet."""
    body = json.dumps({k: v for k, v in row.items() if k in PRE030_COLS},
                      sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((body + (prev or "")).encode("utf-8")).hexdigest()


def _insert_raw(conn, *, text, prev, hash_, origin="unknown", **over):
    """Write a row straight past insert_utterance, the way a pre-030 build did
    (or the way someone editing the file would)."""
    row = {"session_id": "s", "project": "p", "plan_id": "P1", "text": text,
           "occurred_at": "2026-09-15 10:00:00", "recorded_at": "2026-09-15 10:00:01",
           "visibility": "personal", "origin": origin}
    row.update(over)
    row["prev_hash"] = prev
    row["hash"] = hash_
    cols, ph = ", ".join(row), ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO utterance ({cols}) VALUES ({ph})", tuple(row.values()))
    return row


def _write_pre030(conn, text, prev=None, **over):
    """One row exactly as a build older than 030 would have left it."""
    row = {"session_id": "s", "project": "p", "plan_id": "P1", "text": text,
           "occurred_at": "2026-09-15 10:00:00", "recorded_at": "2026-09-15 10:00:01",
           "visibility": "personal"}
    row.update(over)
    return _insert_raw(conn, text=text, prev=prev, hash_=_pre030_hash(row, prev), **over)


# ── the ledger that straddles the migration still verifies ──────────────────

def test_rows_written_before_030_still_verify(conn):
    a = _write_pre030(conn, "the first thing anyone said")
    _write_pre030(conn, "and the second", prev=a["hash"])

    got = pv.verify_chain(conn, "utterance")
    assert got["ok"], got
    assert got["rows"] == 2


def test_rows_written_after_030_verify_on_the_current_form(conn):
    pv.insert_utterance(conn, session_id="s", project="p", plan_id="P1",
                        text="keep fiscal weeks", occurred_at="2026-09-15 10:00:00", origin="hook")
    got = pv.verify_chain(conn, "utterance")
    assert got["ok"], got


def test_a_ledger_can_hold_both_forms_at_once(conn):
    """The real case: rows from before the migration, then rows from after it."""
    old = _write_pre030(conn, "said before the upgrade")
    conn.commit()
    pv.insert_utterance(conn, session_id="s", project="p", plan_id="P1",
                        text="said after it", occurred_at="2026-09-15 11:00:00", origin="hook")

    got = pv.verify_chain(conn, "utterance")
    assert got["ok"], got
    assert got["rows"] == 2
    assert old["hash"] != conn.execute("SELECT hash FROM utterance ORDER BY id DESC LIMIT 1").fetchone()[0]


# ── the fallback is not a hole ──────────────────────────────────────────────

def test_editing_the_words_of_an_old_row_still_breaks_the_chain(conn):
    """The fallback forgives a missing column, never a changed sentence."""
    row = {"session_id": "s", "project": "p", "plan_id": "P1", "text": "what was actually said",
           "occurred_at": "2026-09-15 10:00:00", "recorded_at": "2026-09-15 10:00:01", "visibility": "personal"}
    _insert_raw(conn, text="what someone would rather it said", prev=None,
                hash_=_pre030_hash(row, None))

    got = pv.verify_chain(conn, "utterance")
    assert not got["ok"]
    assert got["first_bad_id"] == 1


def test_the_older_form_cannot_be_used_to_claim_an_origin(conn):
    """The attack the fallback must refuse: hash in the pre-030 form, which omits
    origin, and then write origin='hook' — a record that claims it came from the
    user's keystrokes without ever being hashed with that claim in it."""
    row = {"session_id": "s", "project": "p", "plan_id": "P1", "text": "the user definitely said this",
           "occurred_at": "2026-09-15 10:00:00", "recorded_at": "2026-09-15 10:00:01", "visibility": "personal"}
    _insert_raw(conn, text=row["text"], prev=None, hash_=_pre030_hash(row, None), origin="hook")

    got = pv.verify_chain(conn, "utterance")
    assert not got["ok"], "a pre-030 hash may only carry origin='unknown'"
    assert got["first_bad_id"] == 1


@pytest.mark.parametrize("claimed", ["hook", "human_cli", "agent_cli", "import"])
def test_no_origin_but_unknown_gets_the_fallback(conn, claimed):
    row = {"session_id": "s", "project": "p", "plan_id": "P1", "text": "words",
           "occurred_at": "2026-09-15 10:00:00", "recorded_at": "2026-09-15 10:00:01", "visibility": "personal"}
    _insert_raw(conn, text=row["text"], prev=None, hash_=_pre030_hash(row, None), origin=claimed)
    assert not pv.verify_chain(conn, "utterance")["ok"]


# ── and it says so, rather than looking uniformly fine ──────────────────────

def test_the_report_counts_the_rows_it_accepted_in_the_older_form(conn):
    """A ledger that predates the migration is a fact about that ledger. Passing
    silently would make 'verified' mean two different things."""
    a = _write_pre030(conn, "one")
    _write_pre030(conn, "two", prev=a["hash"])
    conn.commit()
    pv.insert_utterance(conn, session_id="s", project="p", plan_id="P1",
                        text="three", occurred_at="2026-09-15 12:00:00", origin="hook")

    got = pv.verify_chain(conn, "utterance")
    assert got["ok"]
    assert got["rows"] == 3
    assert got["older_form"] == 2, "the two older rows are named as older, not folded into the total"


def test_a_ledger_written_entirely_after_030_reports_none(conn):
    pv.insert_utterance(conn, session_id="s", project="p", plan_id="P1",
                        text="only ever the new form", occurred_at="2026-09-15 10:00:00", origin="hook")
    got = pv.verify_chain(conn, "utterance")
    assert got["ok"] and got["older_form"] == 0


# ── nothing else changed ────────────────────────────────────────────────────

@pytest.mark.parametrize("table", ["reference", "change_reason", "declared_node", "occurrence"])
def test_the_other_chains_have_no_fallback_and_are_unaffected(conn, table):
    """origin exists on exactly one table. The fallback must not leak into the
    others, where a hash mismatch has no innocent explanation."""
    got = pv.verify_chain(conn, table)
    assert got["ok"] and got["older_form"] == 0


def test_the_rendered_report_says_a_ledger_predates_the_column(conn):
    """The count is only worth computing if a reader sees it. A line that says
    'ok' on a ledger half of which was hashed under an older rule is a line that
    means two different things."""
    from orchestrator import integrity

    a = _write_pre030(conn, "one")
    _write_pre030(conn, "two", prev=a["hash"])
    conn.commit()
    pv.insert_utterance(conn, session_id="s", project="p", plan_id="P1",
                        text="three", occurred_at="2026-09-15 12:00:00", origin="hook")

    text = integrity.render(integrity.verify(conn))
    utt = next(l for l in text.splitlines() if l.strip().startswith("chain utterance"))
    assert "2 row(s) predate the origin column" in utt, utt
    ref = next(l for l in text.splitlines() if l.strip().startswith("chain reference"))
    assert "predate" not in ref, "a chain with no older rows says nothing extra"
