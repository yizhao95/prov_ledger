"""A2, second half: pointers rot, and a rotted pointer is itself a record.

`reference.last_checked` says WHEN someone last opened the link. `reference_check`
says WHAT they found, every time, append-only. The difference matters: a dead link
must not quietly disappear and must not be quietly revived either. It becomes
"this pointer stopped opening on this date", which is a dated finding a later
check cannot reverse — it can only add another dated finding beside it.

Four verdicts, and `no_access` is deliberately not `gone`: a document behind a
login this checker does not hold is still there, and saying otherwise would retire
a live reference on the strength of a missing credential.

The opening is done by the host, because the host is what holds the credentials.
`pending` exists to tell it what is worth opening: `linked` rows only. An
`unreachable` row has nothing to open and a `verbal` one never did, so listing
either would be asking the host to go and check a link that does not exist.
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import provenance as pv

REPO = Path(__file__).resolve().parents[2]
AT = "2026-09-15 09:00:00"
VERDICTS = ("ok", "gone", "moved", "no_access")


def _run(conn, *argv):
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"))
    return subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv],
                          capture_output=True, text=True, env=env, cwd=str(REPO))


def _ref(conn, *, kind="email", uri="https://outlook/items/AAQk", label="re: Q3 scope", project="proj"):
    return pv.insert_reference(conn, project=project, kind=kind, label=label, occurred_at=AT, uri=uri)


def _checks(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM reference_check ORDER BY id")]


def _reference_row(conn, ref) -> dict:
    return dict(conn.execute("SELECT * FROM reference WHERE id = ?", (ref,)).fetchone())


# ── (a) one check, one row ──────────────────────────────────────────────────

def test_marking_a_pointer_gone_appends_exactly_one_check(conn):
    ref = _ref(conn)
    r = _run(conn, "reference", "mark", str(ref), "--gone", "--json")
    assert r.returncode == 0, r.stderr

    rows = _checks(conn)
    assert len(rows) == 1
    assert (rows[0]["reference_id"], rows[0]["verdict"], rows[0]["note"]) == (ref, "gone", None)
    assert json.loads(r.stdout)["verdict"] == "gone"


@pytest.mark.parametrize("flag, verdict", [("--ok", "ok"), ("--gone", "gone"),
                                           ("--moved", "moved"), ("--no-access", "no_access")])
def test_each_of_the_four_verdicts_can_be_recorded(conn, flag, verdict):
    ref = _ref(conn)
    r = _run(conn, "reference", "mark", str(ref), flag, "--note", "what the checker saw", "--json")
    assert r.returncode == 0, r.stderr
    rows = _checks(conn)
    assert (rows[0]["verdict"], rows[0]["note"]) == (verdict, "what the checker saw")


def test_no_verdict_at_all_is_refused_and_writes_nothing(conn):
    ref = _ref(conn)
    r = _run(conn, "reference", "mark", str(ref))
    assert r.returncode == 2, r.stdout
    assert all(f"--{v.replace('_', '-')}" in r.stderr for v in VERDICTS), r.stderr
    assert _checks(conn) == [] and _reference_row(conn, ref)["last_checked"] is None


def test_two_verdicts_at_once_is_refused(conn):
    ref = _ref(conn)
    r = _run(conn, "reference", "mark", str(ref), "--ok", "--gone")
    assert r.returncode == 2, r.stdout
    assert "--gone" in r.stderr and "--ok" in r.stderr, r.stderr
    assert _checks(conn) == []


def test_marking_a_reference_that_does_not_exist_is_refused(conn):
    r = _run(conn, "reference", "mark", "4242", "--ok")
    assert r.returncode == 2, r.stdout
    assert "4242" in r.stderr, r.stderr
    assert _checks(conn) == []


# ── (b, c) last_checked moves; nothing else may ─────────────────────────────

def test_the_reference_row_changes_in_exactly_one_column(conn):
    ref = _ref(conn)
    before = _reference_row(conn, ref)
    assert before["last_checked"] is None

    assert _run(conn, "reference", "mark", str(ref), "--gone").returncode == 0
    after = _reference_row(conn, ref)

    assert after["last_checked"] is not None, "the check has to be dated on the reference too"
    assert len(after["last_checked"]) == 19
    assert {k: v for k, v in after.items() if k != "last_checked"} == \
           {k: v for k, v in before.items() if k != "last_checked"}
    # the verdict lives in reference_check; the reference keeps saying what it always said
    assert after["verifiability"] == "linked" and after["uri"] == before["uri"]


@pytest.mark.parametrize("column, value", [("label", "something else"), ("uri", "https://elsewhere"),
                                           ("verifiability", "unreachable"), ("kind", "doc"),
                                           ("project", "other"), ("occurred_at", "2020-01-01 00:00:00"),
                                           ("visibility", "personal"), ("hash", "deadbeef")])
def test_every_other_column_still_refuses_to_change(conn, column, value):
    """The whitelist trigger is what makes `last_checked` safe to write at all.
    A check must not become a back door into editing the pointer itself."""
    ref = _ref(conn)
    assert _run(conn, "reference", "mark", str(ref), "--ok").returncode == 0
    with pytest.raises(sqlite3.IntegrityError, match="only last_checked may change"):
        conn.execute(f"UPDATE reference SET {column} = ? WHERE id = ?", (value, ref))


def test_a_checked_at_of_the_check_and_the_last_checked_of_the_row_agree(conn):
    ref = _ref(conn)
    assert _run(conn, "reference", "mark", str(ref), "--moved", "--json").returncode == 0
    assert _checks(conn)[0]["checked_at"] == _reference_row(conn, ref)["last_checked"]


# ── (d) pending asks the host to open only what can be opened ───────────────

def test_pending_lists_linked_rows_that_were_never_checked(conn):
    linked = _ref(conn, label="linked and unchecked")
    unreachable = _ref(conn, uri=None, label="no link anyone has found")
    verbal = pv.insert_reference(conn, project="proj", kind="verbal", label="hallway with the CFO", occurred_at=AT)

    r = _run(conn, "reference", "pending", "--project", "proj", "--json")
    assert r.returncode == 0, r.stderr
    ids = [row["id"] for row in json.loads(r.stdout)["pending"]]
    assert ids == [linked]
    assert unreachable not in ids, "there is no link to open, so there is nothing to check"
    assert verbal not in ids, "a verbal source never had a link"


def test_pending_drops_a_row_that_was_just_checked_and_keeps_a_stale_one(conn):
    fresh = _ref(conn, label="checked today")
    stale = _ref(conn, label="checked long ago")
    assert _run(conn, "reference", "mark", str(fresh), "--ok").returncode == 0
    assert _run(conn, "reference", "mark", str(stale), "--ok").returncode == 0
    # age the stale one past the threshold; last_checked is the one writable column
    conn.execute("UPDATE reference SET last_checked = '2020-01-01 00:00:00' WHERE id = ?", (stale,))
    conn.commit()

    r = _run(conn, "reference", "pending", "--project", "proj", "--older-than-days", "30", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert [row["id"] for row in out["pending"]] == [stale]
    assert out["older_than_days"] == 30, "the threshold the list was computed with is stated, not implied"


def test_pending_says_which_command_would_record_the_answer(conn):
    ref = _ref(conn)
    r = _run(conn, "reference", "pending", "--project", "proj", "--json")
    row = json.loads(r.stdout)["pending"][0]
    assert row["uri"] == "https://outlook/items/AAQk"
    assert f"reference mark {ref}" in row["mark_with"], row


def test_pending_on_an_empty_ledger_says_so_rather_than_failing(conn):
    r = _run(conn, "reference", "pending", "--project", "proj", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["pending"] == []


def test_a_gone_pointer_is_still_listed_so_it_can_be_looked_at_again(conn):
    """`gone` retires nothing. The row keeps saying `linked`, the check says what
    happened on a date, and the pointer comes back round for another look."""
    ref = _ref(conn)
    assert _run(conn, "reference", "mark", str(ref), "--gone").returncode == 0
    conn.execute("UPDATE reference SET last_checked = '2020-01-01 00:00:00' WHERE id = ?", (ref,))
    conn.commit()
    r = _run(conn, "reference", "pending", "--project", "proj", "--json")
    assert [row["id"] for row in json.loads(r.stdout)["pending"]] == [ref]


# ── (e, f) the checks are a chain, and it only ever grows ───────────────────

def test_the_second_check_appends_next_to_the_first_and_links_to_it(conn):
    ref = _ref(conn)
    assert _run(conn, "reference", "mark", str(ref), "--gone", "--note", "404 from Outlook").returncode == 0
    assert _run(conn, "reference", "mark", str(ref), "--ok", "--note", "it is back").returncode == 0

    rows = _checks(conn)
    assert [r["verdict"] for r in rows] == ["gone", "ok"], "a later check is a new row, never a rewrite"
    assert [r["note"] for r in rows] == ["404 from Outlook", "it is back"]
    assert rows[0]["prev_hash"] is None
    assert rows[1]["prev_hash"] == rows[0]["hash"], "the second check names the one before it"
    assert rows[0]["hash"] != rows[1]["hash"]


def test_every_check_row_carries_the_hash_the_store_computes(conn):
    ref_a, ref_b = _ref(conn, label="a"), _ref(conn, label="b")
    for ref, flag in ((ref_a, "--ok"), (ref_b, "--no-access"), (ref_a, "--gone")):
        assert _run(conn, "reference", "mark", str(ref), flag).returncode == 0

    prev = None
    for row in _checks(conn):
        assert row["prev_hash"] == prev
        assert row["hash"] == pv.chain_hash(prev, row), f"check #{row['id']} is not on the chain"
        prev = row["hash"]
    assert pv.verify_chain(conn, "reference_check")["ok"]
    assert pv.verify_chain(conn, "reference_check")["rows"] == 3


def test_a_check_cannot_be_edited_or_removed_afterwards(conn):
    ref = _ref(conn)
    assert _run(conn, "reference", "mark", str(ref), "--gone").returncode == 0
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE reference_check SET verdict = 'ok'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM reference_check")


def test_the_reference_chain_is_untouched_by_a_check(conn):
    """`last_checked` is outside `canonical` on purpose, so writing it must not
    disturb the chain the reference rows are on."""
    _ref(conn, label="one")
    _ref(conn, label="two")
    before = pv.verify_chain(conn, "reference")
    assert _run(conn, "reference", "mark", "1", "--ok").returncode == 0
    assert pv.verify_chain(conn, "reference") == before
