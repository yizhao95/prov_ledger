"""`provledger record <cite>` — one record, whole and uncut (spec §10.2).

Every existing read truncates. Measured on the live ledger: `#274` is 516
characters and ends at "…which auto-fires r"; `#790` is 994 and ends at
"…api's regis". `provledger reason` offers only `mark`, which is a WRITE. So
the end of a recorded sentence was unreachable by any read, and in one
evaluation that cost something real: an agent dropped a piece of evidence from
a reply because it could not read the end of it.

§10.2 asks for exactly two things — the graph and its relations, and one
documented way to take a node's history. This is the bottom of that second
one: whatever a `why` line or a `graph` row points at, this opens in full,
under the cite forms the product already prints (`#12` a ledger record, `#r3`
a source) rather than a new spelling invented for the occasion.
"""
import pytest

from orchestrator import provenance as pv, record_read

LONG = ("Step B: dropping a default was invisible because the fingerprint's new `defaulted` list did not take part "
        "in the compare — it does now; and the FL-019 script test completed the recovery sub-step through "
        "complete-step, which auto-fires review_and_complete and re-opens the plan by itself (correct), so "
        "agent-review-close found it already COMPLETED. Retry under the FAILED B.")


@pytest.fixture
def seeded(conn):
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P1",
                            text="keep load_orders on paid orders only, finance signed it off",
                            occurred_at="2026-09-15 10:00:00", origin="hook", visibility="shareable")
    stated = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical",
                              verbatim=(u, 0, 36), recorded_by="agent", occurred_at="2026-09-15 10:05:00")
    long_id = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical",
                               role="rejected_path", interpretation=LONG, rationale="the private half",
                               rule_id="R6", recorded_by="system", step_id="P0-B")
    ref = pv.insert_reference(conn, project="proj", kind="doc", label="finance sign-off",
                              occurred_at="2026-09-14 09:00:00", uri="https://example.invalid/f")
    verbal = pv.insert_reference(conn, project="proj", kind="verbal", label="said in standup",
                                 occurred_at="2026-09-14 09:30:00")
    pv.link_reference(conn, stated, ref, stance="supports")
    pv.link_reference(conn, long_id, verbal, stance="contradicts")
    pv.insert_reference_check(conn, reference_id=ref, verdict="gone", note="404 since the wiki move")
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment) VALUES (?, 'proj', 'P2', 'why')", (long_id,))
    conn.execute("INSERT INTO influence (reason_id, project, plan_id, via, by) VALUES (?, 'proj', 'P3', 'reason_because', 'agent')", (long_id,))
    conn.commit()
    return {"u": u, "stated": stated, "long": long_id, "ref": ref, "verbal": verbal}


def test_the_text_no_other_read_could_reach_comes_out_entire(conn, seeded):
    doc = record_read.record(conn, f"#{seeded['long']}")
    assert doc["text"] == LONG and doc["text_chars"] == len(LONG)
    assert doc["text"].endswith("Retry under the FAILED B.")       # the tail every other read cut off
    assert LONG in record_read.render(doc)


def test_the_four_cite_spellings_the_product_already_prints_all_resolve(conn, seeded):
    rid, ref = seeded["long"], seeded["ref"]
    assert record_read.record(conn, f"#{rid}")["id"] == rid
    assert record_read.record(conn, str(rid))["id"] == rid
    assert record_read.record(conn, f"#r{ref}")["id"] == ref
    assert record_read.record(conn, f"r{ref}")["id"] == ref
    assert record_read.record(conn, f"#r{ref}")["cite_kind"] == "source"
    assert record_read.record(conn, f"#{rid}")["cite_kind"] == "record"
    assert record_read.record(conn, f"#r{ref}")["kind"] == "doc"        # `kind` stays the recorded column


def test_a_record_carries_its_tier_source_level_dates_author_node_and_plan(conn, seeded):
    doc = record_read.record(conn, f"#{seeded['long']}")
    # a rule_id makes the tier `derived` — the tier is derived from the inputs and
    # there is deliberately no way to pass it (provenance.insert_reason)
    assert doc["tier"] == "derived" and doc["evidence_level"] == "verbal"
    assert doc["role"] == "rejected_path" and doc["state"] == "active" and doc["rule_id"] == "R6"
    assert doc["recorded_by"] == "system" and doc["occurred_at"] and doc["recorded_at"]
    assert doc["node_key"] == "nk_a" and doc["plan_id"] == "P0" and doc["step_id"] == "P0-B"
    assert doc["shown"] == 1 and doc["adopted_by"] == ["P3"]
    text = record_read.render(doc)
    assert "derived" in text and "nk_a" in text and "P0-B" in text and "shown 1" in text


def test_a_stated_record_shows_the_utterance_and_the_span_it_quotes(conn, seeded):
    doc = record_read.record(conn, f"#{seeded['stated']}")
    assert doc["tier"] == "stated"
    q = doc["utterance"]
    assert q["id"] == seeded["u"] and q["origin"] == "hook" and q["span"] == [0, 36]
    assert q["quoted"] == "keep load_orders on paid orders only"
    assert q["text"].endswith("finance signed it off")              # the words around the span, too
    text = record_read.render(doc)
    assert "keep load_orders on paid orders only" in text and "hook" in text


def test_the_references_hanging_on_a_record_come_with_their_own_cites(conn, seeded):
    doc = record_read.record(conn, f"#{seeded['long']}")
    assert doc["references"] == [{"cite": f"#r{seeded['verbal']}", "kind": "verbal", "label": "said in standup",
                                 "uri": None, "stance": "contradicts", "verifiability": "verbal", "last_checked": None}]
    assert f"#r{seeded['verbal']}" in record_read.render(doc)


def test_a_source_read_says_what_was_found_when_it_was_opened_and_what_hangs_on_it(conn, seeded):
    doc = record_read.record(conn, f"#r{seeded['ref']}")
    assert doc["cite_kind"] == "source" and doc["label"] == "finance sign-off" and doc["verifiability"] == "linked"
    assert doc["uri"] == "https://example.invalid/f" and doc["last_checked"]
    assert [c["verdict"] for c in doc["checks"]] == ["gone"] and doc["checks"][0]["note"] == "404 since the wiki move"
    assert doc["anchors"] == [{"cite": f"#{seeded['stated']}", "stance": "supports"}]
    text = record_read.render(doc)
    assert "gone" in text and "404 since the wiki move" in text and f"#{seeded['stated']}" in text


def test_a_personal_rationale_is_shown_with_its_visibility_named(conn, seeded):
    """A local read is not an export: `export` refuses personal rows by code and
    that stays true. Here the words are shown and labelled, so the reader knows
    what may not travel rather than finding a blank where a sentence was."""
    doc = record_read.record(conn, f"#{seeded['long']}")
    assert doc["rationale"] == "the private half" and doc["rationale_visibility"] == "personal"
    assert "the private half" in record_read.render(doc) and "personal" in record_read.render(doc)


def test_an_unknown_cite_says_so_rather_than_guessing(conn, seeded):
    assert record_read.record(conn, "#99999") is None
    assert record_read.record(conn, "#r99999") is None
    with pytest.raises(ValueError, match="cite"):
        record_read.record(conn, "not-a-cite")
    assert "99999" in record_read.render(None, "#99999")


def test_the_record_read_writes_nothing_at_all(conn, seeded):
    """It is a READ. `why` counts a showing (FL-150, under review); this must
    not, or the act of reading a row whole would change what the ledger says
    about how often it was read."""
    tables = ("change_reason", "read_hit", "influence", "utterance", "reference", "reference_check", "reference_link")
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables]
    record_read.record(conn, f"#{seeded['long']}")
    record_read.record(conn, f"#{seeded['stated']}")
    record_read.record(conn, f"#r{seeded['ref']}")
    after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables]
    assert before == after
    assert conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0] == 1      # the one the fixture wrote


# ── the CLI entry ────────────────────────────────────────────────────────────

def test_cli_record_prints_the_whole_row_and_a_source_and_offers_json(conn, seeded):
    import os
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(repo / "orchestrator-backend"))

    def cli(*argv):
        return subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True,
                              env=env, cwd=str(repo))
    r = cli("record", f"#{seeded['long']}")
    assert r.returncode == 0, r.stderr
    assert LONG in r.stdout and "Retry under the FAILED B." in r.stdout
    src = cli("record", f"#r{seeded['ref']}")
    assert src.returncode == 0 and "finance sign-off" in src.stdout and "gone" in src.stdout
    j = cli("record", str(seeded["long"]), "--json")
    assert j.returncode == 0 and '"text_chars"' in j.stdout
    missing = cli("record", "#99999")
    assert missing.returncode == 1 and "99999" in (missing.stdout + missing.stderr)
    bad = cli("record", "nonsense")
    assert bad.returncode == 2 and "cite" in bad.stderr
    assert conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0] == 1        # unchanged by three reads


def test_the_footer_names_the_task_that_produced_the_record_with_its_real_id():
    """The hop a model will not take unless it is handed the command.

    A record prints the plan and step that produced it, and the task's steps and
    logs are where the substance usually is — a failure reason, what was measured,
    what was decided. But the footer listed only `record`, `why` and `graph`, so the
    plan id sat on screen as a label with nothing saying it was a door.

    Verified live: `record '#3403'` prints `plan dp6-a-20260927063613 · step
    dp6-a-20260927063613-REVIEW.1.2`, and that plan's step log holds the whole
    derivation of a constant that nothing else in the ledger states. The footer has
    to carry the id, not a `<plan-id>` placeholder — a placeholder makes the reader
    go and find what is already on the screen above it.
    """
    from orchestrator import record_read

    doc = {"cite": "#7", "cite_kind": "record", "role": "reason", "tier": "asserted",
           "evidence_level": "task_context", "kind": "technical", "state": "active",
           "node_key": "nk_abc", "plan_id": "plan-xyz-123", "step_id": "plan-xyz-123-B",
           "occurred_at": "2026-09-01 00:00", "recorded_at": "2026-09-01 00:00",
           "recorded_by": "agent", "shown": 0, "shown_by_moment": {}, "adopted_by": [],
           "text": "a reason", "text_chars": 8, "references": [], "influence": []}
    out = record_read.render(doc)
    assert "provledger plan plan-xyz-123" in out, \
        "the footer must name the task read with the plan id this record carries"
    assert "<plan-id>" not in out, "a placeholder sends the reader looking for what is already printed"


def test_the_footer_omits_the_task_read_when_the_record_has_no_plan():
    from orchestrator import record_read

    doc = {"cite": "#8", "cite_kind": "record", "role": "rejected_path", "tier": "derived",
           "evidence_level": "unstated", "kind": "technical", "state": "active",
           "node_key": None, "plan_id": None, "step_id": None,
           "occurred_at": "2026-09-01 00:00", "recorded_at": "2026-09-01 00:00",
           "recorded_by": "system", "shown": 0, "shown_by_moment": {}, "adopted_by": [],
           "text": "a floating rejected path", "text_chars": 24, "references": [], "influence": []}
    out = record_read.render(doc)
    assert "provledger plan" not in out, "no plan means no task read to offer"


def test_a_deviation_cite_is_refused_with_the_read_that_does_hold_it():
    """`plan` prints deviations as `#v94` (see test_plan_read: a bare `#94` collided
    with a ledger record and produced a silent wrong answer). A reader handed `#v94`
    will try `record` first, because that is the read for cites. Refusing is right —
    a deviation is not a record — but a refusal that does not say where the thing
    IS leaves them stuck holding a cite nothing reads."""
    from orchestrator import record_read

    msg = record_read.cite_error("#v94")
    assert "provledger plan" in msg, "a refusal must name the read that holds a deviation"
    assert "#v94" in msg

