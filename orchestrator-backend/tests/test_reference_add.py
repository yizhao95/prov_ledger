"""A2: `provledger reference add` — a pointer to something outside the ledger.

What a reference is: a label, a time, and — when someone can produce one — a
permalink; since step 3b of the task-level redesign also who said it and the
passage they said (`--said-by`, `--excerpt`), because a link rots and the words
are what a reader needs. The passage, not the document: at most 2000 characters,
as much as the agent that found it judged was needed.

provLedger never fetches the uri. Finding the link is the host agent's job, with
the host agent's credentials; this command only records what it was handed. So a
reference with no uri says `unreachable` and stays that way until someone finds
one — it does not pretend, and it is not dropped either.

A pointer hangs on exactly one thing: the reason it backs, or — when the words
have been said but no plan exists yet — the utterance that named the source.
`stance` is the other half: evidence is allowed to disagree with the reason it is
attached to, because the alternative is discarding it, and silent discarding is
the thing this project refuses to do.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import provenance as pv

REPO = Path(__file__).resolve().parents[2]
AT = "2026-09-15 09:00:00"
POINTER_MSG = "a pointer, not a body"


def _run(conn, *argv, env_extra=None):
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"))
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv],
                          capture_output=True, text=True, env=env, cwd=str(REPO))


@pytest.fixture
def anchors(conn):
    """One utterance and one reason, the two things a pointer may hang on."""
    uid = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P1",
                              text="Sarah emailed that Q3 excludes EMEA", occurred_at=AT, origin="hook")
    rid = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="organizational",
                           interpretation="Q3 excludes EMEA")
    return {"utterance": uid, "reason": rid}


def _add(conn, *extra, kind="email", label="re: Q3 scope · sarah@example.com"):
    return _run(conn, "reference", "add", "--json", "--project", "proj", "--kind", kind,
                "--label", label, "--occurred-at", AT, *extra)


def _rows(conn, table="reference"):
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]


# ── (a, b) a link, or the honest absence of one ──────────────────────────────

def test_a_uri_makes_the_reference_linked(conn, anchors):
    r = _add(conn, "--reason", str(anchors["reason"]), "--uri", "https://outlook/items/AAQk")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    row = conn.execute("SELECT uri, verifiability, kind, label, occurred_at FROM reference WHERE id = ?",
                       (out["reference_id"],)).fetchone()
    assert row["uri"] == "https://outlook/items/AAQk" and row["verifiability"] == "linked"
    assert (row["kind"], row["occurred_at"]) == ("email", AT)
    assert out["verifiability"] == "linked"


def test_no_uri_says_unreachable_rather_than_pretending(conn, anchors):
    """The email is real; nobody has produced a link to it. 'unreachable' is the
    true statement, and the record still exists so the link can be added later."""
    r = _add(conn, "--reason", str(anchors["reason"]))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    row = conn.execute("SELECT uri, verifiability FROM reference WHERE id = ?", (out["reference_id"],)).fetchone()
    assert (row["uri"], row["verifiability"]) == (None, "unreachable")
    assert out["verifiability"] == "unreachable"


FETCHERS = ("urlopen", "urllib", "requests", "httpx", "WebFetch", "socket", "curl")


def test_nothing_in_the_command_fetches_the_uri(conn, anchors):
    """Read the source, not the behaviour: a command that reached out would put
    provLedger's process in front of the host's credentials, which is exactly the
    thing the host agent is there to keep hold of."""
    src = (REPO / "orchestrator-backend" / "orchestrator" / "cli.py").read_text(encoding="utf-8")
    assert "def _reference_add" in src, "expected a _reference_add in cli.py"
    section = src.split("def _reference_add", 1)[1].split("def _reference_cmd", 1)[0]
    for fetcher in FETCHERS:
        assert fetcher not in section, f"reference add must not reach for {fetcher}"
        assert fetcher not in src, f"the provledger CLI must not reach for {fetcher}"
    store = (REPO / "orchestrator-backend" / "orchestrator" / "provenance.py").read_text(encoding="utf-8")
    for fetcher in FETCHERS:
        assert fetcher not in store, f"the store must not reach for {fetcher}"


# ── (c) evidence may disagree ────────────────────────────────────────────────

def test_stance_defaults_to_supports(conn, anchors):
    r = _add(conn, "--reason", str(anchors["reason"]))
    assert r.returncode == 0, r.stderr
    ref = json.loads(r.stdout)["reference_id"]
    assert conn.execute("SELECT stance FROM reference_link WHERE reference_id = ?", (ref,)).fetchone()[0] == "supports"


def test_a_contradicting_email_is_recorded_as_contradicting(conn, anchors):
    r = _add(conn, "--reason", str(anchors["reason"]), "--stance", "contradicts",
             label="re: EMEA is in Q3 after all · sarah@example.com")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["stance"] == "contradicts"
    assert conn.execute("SELECT stance FROM reference_link WHERE reference_id = ?",
                        (out["reference_id"],)).fetchone()[0] == "contradicts"


@pytest.mark.parametrize("stance", ["supports", "contradicts", "context"])
def test_all_three_stances_are_accepted(conn, anchors, stance):
    r = _add(conn, "--reason", str(anchors["reason"]), "--stance", stance)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["stance"] == stance


def test_a_stance_outside_the_three_is_refused(conn, anchors):
    r = _add(conn, "--reason", str(anchors["reason"]), "--stance", "probably")
    assert r.returncode == 2, r.stdout
    assert "--stance" in r.stderr and "probably" in r.stderr, r.stderr


# ── (d) one anchor, never two ───────────────────────────────────────────────

def test_reason_attaches_with_the_utterance_half_left_null(conn, anchors):
    r = _add(conn, "--reason", str(anchors["reason"]))
    assert r.returncode == 0, r.stderr
    link = conn.execute("SELECT reason_id, utterance_id FROM reference_link WHERE reference_id = ?",
                        (json.loads(r.stdout)["reference_id"],)).fetchone()
    assert (link["reason_id"], link["utterance_id"]) == (anchors["reason"], None)


def test_utterance_attaches_with_the_reason_half_left_null(conn, anchors):
    """The cheap path (A3): the sentence named a source and no plan exists yet, so
    the pointer hangs on the words until a reason is there to carry it."""
    r = _add(conn, "--utterance", str(anchors["utterance"]))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    link = conn.execute("SELECT reason_id, utterance_id FROM reference_link WHERE reference_id = ?",
                        (out["reference_id"],)).fetchone()
    assert (link["reason_id"], link["utterance_id"]) == (None, anchors["utterance"])
    assert out["utterance_id"] == anchors["utterance"] and out["reason_id"] is None


def test_both_anchors_at_once_is_refused_with_exit_2(conn, anchors):
    r = _add(conn, "--reason", str(anchors["reason"]), "--utterance", str(anchors["utterance"]))
    assert r.returncode == 2, r.stdout
    assert "--utterance" in r.stderr and "--reason" in r.stderr, r.stderr
    assert conn.execute("SELECT COUNT(*) FROM reference").fetchone()[0] == 0, "a refused command writes nothing"


def test_neither_anchor_is_refused_with_exit_2(conn, anchors):
    r = _add(conn)
    assert r.returncode == 2, r.stdout
    assert "--reason" in r.stderr and "--utterance" in r.stderr, r.stderr
    assert conn.execute("SELECT COUNT(*) FROM reference").fetchone()[0] == 0


def test_an_anchor_that_does_not_exist_is_refused_and_writes_nothing(conn, anchors):
    r = _add(conn, "--reason", "9999")
    assert r.returncode == 2, r.stdout
    assert "9999" in r.stderr, r.stderr
    assert conn.execute("SELECT COUNT(*) FROM reference").fetchone()[0] == 0


# ── (e) a label is a pointer ────────────────────────────────────────────────

def test_a_label_over_512_characters_is_refused_with_the_existing_message(conn, anchors):
    r = _add(conn, "--reason", str(anchors["reason"]), label="x" * 513)
    assert r.returncode == 2, r.stdout
    assert POINTER_MSG in r.stderr, r.stderr
    assert conn.execute("SELECT COUNT(*) FROM reference").fetchone()[0] == 0


def test_a_label_of_exactly_512_characters_is_fine(conn, anchors):
    r = _add(conn, "--reason", str(anchors["reason"]), label="x" * 512)
    assert r.returncode == 0, r.stderr
    assert conn.execute("SELECT length(label) FROM reference").fetchone()[0] == 512


def test_an_empty_label_is_refused_with_the_same_message(conn, anchors):
    r = _add(conn, "--reason", str(anchors["reason"]), label="")
    assert r.returncode == 2, r.stdout
    assert POINTER_MSG in r.stderr, r.stderr


# ── (f) a reason may hang as many pointers as it has ────────────────────────

def test_five_references_on_one_reason_are_all_kept(conn, anchors):
    """The user's word on this was "hang them all". Many-to-many is the shape the
    table already has; nothing here may quietly keep only the last one."""
    kinds = ["email", "meeting", "chat", "ticket", "doc"]
    ids = []
    for i, kind in enumerate(kinds):
        stance = ("--stance", "contradicts") if kind == "chat" else ()
        r = _add(conn, "--reason", str(anchors["reason"]), *stance, kind=kind, label=f"source {i}")
        assert r.returncode == 0, r.stderr
        ids.append(json.loads(r.stdout)["reference_id"])

    assert len(set(ids)) == 5
    got = conn.execute("SELECT f.kind, l.stance FROM reference_link l JOIN reference f ON f.id = l.reference_id "
                       "WHERE l.reason_id = ? ORDER BY f.id", (anchors["reason"],)).fetchall()
    assert [r[0] for r in got] == kinds
    assert [r[1] for r in got] == ["supports", "supports", "contradicts", "supports", "supports"]


# ── (g) the eight kinds and nothing else ────────────────────────────────────

@pytest.mark.parametrize("kind", list(pv.REFERENCE_KINDS))
def test_every_kind_the_store_knows_is_accepted(conn, anchors, kind):
    r = _add(conn, "--reason", str(anchors["reason"]), kind=kind)
    assert r.returncode == 0, r.stderr
    assert conn.execute("SELECT kind FROM reference").fetchone()[0] == kind


def test_a_kind_outside_the_enum_is_refused(conn, anchors):
    assert len(pv.REFERENCE_KINDS) == 8
    r = _add(conn, "--reason", str(anchors["reason"]), kind="telepathy")
    assert r.returncode == 2, r.stdout
    assert "telepathy" in r.stderr, r.stderr
    assert conn.execute("SELECT COUNT(*) FROM reference").fetchone()[0] == 0


def test_a_verbal_reference_with_a_uri_is_refused(conn, anchors):
    """`verbal` means there is nothing to open. A uri beside it is a contradiction
    the store already rejects; the command must not launder it."""
    r = _add(conn, "--reason", str(anchors["reason"]), "--uri", "https://outlook/x", kind="verbal")
    assert r.returncode == 2, r.stdout
    assert "verbal reference has no uri" in r.stderr, r.stderr
    assert conn.execute("SELECT COUNT(*) FROM reference").fetchone()[0] == 0


# ── (h) the row joins the chain the same way every other row does ───────────

def test_the_chain_hash_is_the_one_insert_reference_computes(conn, anchors):
    a = _add(conn, "--reason", str(anchors["reason"]), label="first")
    b = _add(conn, "--utterance", str(anchors["utterance"]), "--uri", "https://tracker/DATA-42",
             label="second")
    assert (a.returncode, b.returncode) == (0, 0), (a.stderr, b.stderr)

    prev = None
    for row in _rows(conn):
        assert row["prev_hash"] == prev
        assert row["hash"] == pv.chain_hash(prev, row), f"reference #{row['id']} is not on the chain"
        prev = row["hash"]
    assert pv.verify_chain(conn, "reference") == {"ok": True, "rows": 2, "first_bad_id": None, "older_form": 0}


def test_a_reference_the_command_wrote_is_append_only_afterwards(conn, anchors):
    import sqlite3
    r = _add(conn, "--reason", str(anchors["reason"]))
    ref = json.loads(r.stdout)["reference_id"]
    with pytest.raises(sqlite3.IntegrityError, match="only last_checked may change"):
        conn.execute("UPDATE reference SET label = 'something else' WHERE id = ?", (ref,))


# ── (i) who said it and what they said (step 3b) ────────────────────────────

EXCERPT = "From the June release the v2 feed no longer carries orders.discount."


def test_said_by_and_excerpt_are_kept_on_the_row(conn, anchors):
    r = _add(conn, "--utterance", str(anchors["utterance"]), "--uri", "notes/2026-05-19-sync.md",
             "--said-by", "the platform lead", "--excerpt", EXCERPT, kind="meeting", label="platform sync, 2026-05-19")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert (out["said_by"], out["excerpt"]) == ("the platform lead", EXCERPT)
    row = _rows(conn)[0]
    assert (row["said_by"], row["excerpt"]) == ("the platform lead", EXCERPT)
    assert pv.verify_chain(conn, "reference")["ok"] is True


def test_the_excerpt_can_come_from_a_file(conn, anchors, tmp_path):
    f = tmp_path / "passage.txt"
    f.write_text(EXCERPT + "\n", encoding="utf-8")
    r = _add(conn, "--reason", str(anchors["reason"]), "--excerpt-file", str(f))
    assert r.returncode == 0, r.stderr
    assert _rows(conn)[0]["excerpt"] == EXCERPT


def test_an_excerpt_over_2000_characters_is_refused_naming_the_limit(conn, anchors):
    r = _add(conn, "--reason", str(anchors["reason"]), "--excerpt", "w" * 2001)
    assert r.returncode == 2, r.stdout
    assert "2000" in r.stderr, r.stderr
    assert _rows(conn) == []


def test_excerpt_and_excerpt_file_together_are_refused(conn, anchors, tmp_path):
    f = tmp_path / "passage.txt"
    f.write_text(EXCERPT, encoding="utf-8")
    r = _add(conn, "--reason", str(anchors["reason"]), "--excerpt", EXCERPT, "--excerpt-file", str(f))
    assert r.returncode == 2, r.stdout
    assert _rows(conn) == []
