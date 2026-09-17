"""declared — one sentence becomes a node, and its tier is decided by who said what.

The rule this module exists to enforce: `declare()` has no `tier` parameter.
A field the user typed is `stated`, a field the model tidied up is `asserted`,
and the row is never `observed` — nobody observed a meeting decision.

The model is a stub in every test here (the real one is headless `claude`, run
by hand at acceptance time): what is under test is the store and the tiering,
not the model.
"""
import json

import pytest

from orchestrator import provenance


@pytest.fixture
def declared():
    """The module under test, imported per test so its absence is a red test
    rather than a collection error."""
    from orchestrator import declared as mod
    return mod


KNOWN = {"pkg.rollup.quarterly_revenue", "pkg.rollup.emea_share", "pkg.load.orders"}


def _runner(payload):
    def run(prompt, *, model=None, timeout_s=None):
        return payload if isinstance(payload, str) else json.dumps(payload)
    return run


def _utterance(conn, text="EMEA is excluded from the Q3 rollup — the steering group decided on 2026-03-14"):
    return provenance.insert_utterance(conn, session_id="s1", project="demo", plan_id=None, text=text,
                                       occurred_at="2026-03-14 09:00:00")


def test_declare_has_no_tier_parameter(conn, declared):
    """The one signature rule: a caller cannot hand a declared node its tier."""
    import inspect
    assert "tier" not in inspect.signature(declared.declare).parameters


def test_a_direct_declaration_without_a_model_is_stated_in_every_field(conn, declared):
    row = declared.declare(conn, "demo", "EMEA excluded from the Q3 rollup",
                           node_type="business_rule", attrs={"owner": "steering group"},
                           links=[("pkg.rollup.quarterly_revenue", "declared_constrains")],
                           known_names=KNOWN, occurred_at="2026-03-14 09:00:00")
    assert row["state"] == "draft" and row["tier"] == "stated"
    assert row["qualified_name"] == "declared:emea-excluded-from-the-q3-rollup"
    ft = json.loads(row["field_tiers_json"])
    assert ft["node_type"] == "stated" and ft["attrs"]["owner"] == "stated"
    assert ft["links"] == ["stated"]
    assert row["links_checked"] == 1


def test_a_declaration_without_a_model_and_without_a_type_is_refused_not_guessed(conn, declared):
    with pytest.raises(ValueError, match="node_type"):
        declared.declare(conn, "demo", "EMEA excluded from the Q3 rollup", known_names=KNOWN)


def test_a_model_link_outside_the_graph_rejects_the_whole_observation(conn, declared):
    runner = _runner({"node_type": "business_rule", "name": "EMEA excluded from Q3 rollup",
                      "attrs": {"scope": "Q3"},
                      "links": [{"to": "pkg.rollup.quarterly_revenue", "kind": "declared_constrains"},
                                {"to": "pkg.rollup.does_not_exist", "kind": "declared_constrains"}]})
    with pytest.raises(declared.ModelRejected, match="pkg.rollup.does_not_exist"):
        declared.declare(conn, "demo", "EMEA is out of the Q3 rollup", runner=runner, known_names=KNOWN)
    assert conn.execute("SELECT COUNT(*) FROM declared_node").fetchone()[0] == 0


def test_a_model_draft_is_asserted_and_confirming_it_makes_the_row_stated(conn, declared):
    runner = _runner({"node_type": "stakeholder_decision", "name": "Q3 rollup excludes EMEA",
                      "attrs": {"decided_on": "2026-03-14"},
                      "links": [{"to": "pkg.rollup.quarterly_revenue", "kind": "declared_constrains"}]})
    draft = declared.declare(conn, "demo", "EMEA is out of the Q3 rollup", runner=runner,
                             known_names=KNOWN, model="stub")
    assert draft["state"] == "draft" and draft["tier"] == "asserted"
    uid = _utterance(conn)
    row = declared.confirm(conn, draft["id"], uid)
    assert row["state"] == "active" and row["tier"] == "stated"
    assert row["description_utterance_id"] == uid
    assert row["version"] == 2 and row["supersedes"] == draft["id"]
    assert conn.execute("SELECT superseded_by FROM declared_node WHERE id = ?", (draft["id"],)).fetchone()[0] == row["id"]


def test_confirming_does_not_relabel_the_edges_the_model_proposed(conn, declared):
    runner = _runner({"node_type": "business_rule", "name": "EMEA excluded from Q3 rollup", "attrs": {},
                      "links": [{"to": "pkg.rollup.emea_share", "kind": "declared_constrains"}]})
    draft = declared.declare(conn, "demo", "EMEA is out of the Q3 rollup", runner=runner, known_names=KNOWN)
    row = declared.confirm(conn, draft["id"], _utterance(conn))
    ft = json.loads(row["field_tiers_json"])
    assert row["tier"] == "stated"
    assert ft["links"] == ["asserted"], "an edge the model proposed stays asserted after the row is confirmed"
    assert ft["node_type"] == "asserted"


def test_revise_appends_a_version_instead_of_changing_the_old_one(conn, declared):
    row = declared.declare(conn, "demo", "EMEA excluded from the Q3 rollup", node_type="business_rule",
                           known_names=KNOWN)
    active = declared.confirm(conn, row["id"], _utterance(conn))
    two = declared.revise(conn, active["id"], attrs={"scope": "Q3 and Q4"}, utterance_id=_utterance(conn, "make it Q3 and Q4"))
    assert two["version"] == 3 and two["slug"] == active["slug"]
    assert json.loads(two["attrs_json"]) == {"scope": "Q3 and Q4"}
    assert conn.execute("SELECT superseded_by FROM declared_node WHERE id = ?", (active["id"],)).fetchone()[0] == two["id"]
    assert [r["id"] for r in declared.active(conn, "demo")] == [two["id"]]


def test_retire_appends_a_retired_row_and_the_node_leaves_the_active_set(conn, declared):
    row = declared.declare(conn, "demo", "EMEA excluded from the Q3 rollup", node_type="business_rule",
                           known_names=KNOWN)
    active = declared.confirm(conn, row["id"], _utterance(conn))
    gone = declared.retire(conn, active["id"], utterance_id=_utterance(conn, "the rule is withdrawn"))
    assert gone["state"] == "retired"
    assert declared.active(conn, "demo") == []
    assert conn.execute("SELECT COUNT(*) FROM declared_node").fetchone()[0] == 3


def test_the_hash_chain_verifies_across_every_version(conn, declared):
    row = declared.declare(conn, "demo", "EMEA excluded from the Q3 rollup", node_type="business_rule",
                           known_names=KNOWN)
    active = declared.confirm(conn, row["id"], _utterance(conn))
    declared.revise(conn, active["id"], attrs={"scope": "Q3"})
    assert provenance.verify_chain(conn, "declared_node") == {"ok": True, "rows": 3, "first_bad_id": None}


def test_a_row_inserted_around_the_store_is_named_by_verify_chain(conn, declared):
    """The triggers stop an UPDATE; the chain catches an INSERT that skipped
    `declared.declare` — the two together are what 'append-only' means here."""
    row = declared.declare(conn, "demo", "EMEA excluded from the Q3 rollup", node_type="business_rule",
                           known_names=KNOWN)
    declared.confirm(conn, row["id"], _utterance(conn))
    conn.execute("INSERT INTO declared_node (project, slug, qualified_name, node_type, description, attrs_json, "
                 "links_json, links_checked, state, tier, field_tiers_json, version, recorded_by, occurred_at, hash) "
                 "VALUES ('demo', 'smuggled', 'declared:smuggled', 'business_rule', 'never said', '{}', '[]', 1, "
                 "'active', 'stated', '{}', 1, 'human', '2026-03-14 09:00:00', 'not-a-real-hash')")
    conn.commit()
    smuggled = conn.execute("SELECT MAX(id) FROM declared_node").fetchone()[0]
    out = provenance.verify_chain(conn, "declared_node")
    assert out["ok"] is False and out["first_bad_id"] == smuggled


def test_a_model_answer_that_is_not_the_agreed_json_is_rejected_whole(conn, declared):
    with pytest.raises(declared.ModelRejected):
        declared.declare(conn, "demo", "EMEA is out", runner=_runner("I think this is a business rule."),
                         known_names=KNOWN)
    with pytest.raises(declared.ModelRejected, match="node_type"):
        declared.declare(conn, "demo", "EMEA is out", known_names=KNOWN,
                         runner=_runner({"node_type": "function", "name": "x", "attrs": {}, "links": []}))
