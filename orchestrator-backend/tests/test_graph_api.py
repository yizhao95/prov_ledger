"""orchestrator.graph_api — the host-owned contract a NodeTypeProvider speaks
(phase 6). Host owns identity, history, tier and propagation; a provider only
extracts observations with signatures and attributes."""
import dataclasses
import sqlite3

import pytest

from orchestrator import graph_api as g


def _obs(**kw):
    base = dict(type_id="acme.example", node_type="dataset", qualified_name="pkg.m.orders", file_path="pkg/m.py",
                line_start=1, line_end=2, signatures=(g.Signature("qualname", "pkg.m.orders"),), attrs={"rows": 3})
    base.update(kw)
    return g.NodeObservation(**base)


def test_dataclasses_are_frozen_and_have_defaults():
    sig = g.Signature(layer="struct", value="abc")
    assert sig.trivial is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        sig.value = "x"
    obs = _obs()
    assert obs.owner_qn is None and obs.name is None and obs.node_id is None and obs.schema_version == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        obs.qualified_name = "y"
    conn = sqlite3.connect(":memory:")
    ctx = g.ExtractionContext(repo_root="/r", conn_ro=conn, run_id=1, file_map={}, node_rows=lambda kinds: [])
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.run_id = 2
    assert g.STABILITY == ("preserved", "broken", "ambiguous")
    assert "rename_function" in g.MUTATIONS and "swap_two_similar" in g.MUTATIONS and len(g.MUTATIONS) == 10
    assert g.KNOWN_LAYERS == ("qualname", "struct", "dataflow")


def test_validate_attrs_reports_required_types_and_forbidden():
    schema = {"required": ["rows"], "types": {"rows": "int", "label": "str"}, "forbidden": ["secret"]}
    assert g.validate_attrs(schema, {"rows": 3, "label": "a"}) == []
    errs = g.validate_attrs(schema, {"label": 5, "secret": 1})
    kinds = {e["kind"] for e in errs}
    assert kinds == {"required", "type", "forbidden"}
    assert any(e["kind"] == "required" and e["attr"] == "rows" for e in errs)
    assert any(e["kind"] == "type" and e["attr"] == "label" and "str" in e["detail"] for e in errs)
    assert any(e["kind"] == "forbidden" and e["attr"] == "secret" for e in errs)
    assert g.validate_attrs({"types": {"n": "float"}}, {"n": 1}) == []            # int is an acceptable float
    assert g.validate_attrs({"types": {"n": "bool"}}, {"n": 1}) != []             # but 1 is not a bool
    assert g.validate_attrs({"types": {"n": "list"}}, {"n": (1, 2)}) != []


def test_tier_is_always_forbidden():
    """A plugin may not declare a tier — tier is decided by the channel (extract == observed)."""
    assert [e["attr"] for e in g.validate_attrs({}, {"tier": "asserted"})] == ["tier"]
    assert [e["attr"] for e in g.validate_attrs({"required": [], "forbidden": []}, {"tier": "observed"})] == ["tier"]
    assert "tier" in g.normalize_schema({})["forbidden"]


def test_provider_protocol_is_duck_typed():
    class P:
        type_id = "acme.example"
        schema_version = 1
        requires = ()
        def extract(self, ctx): return []
        def attributes_schema(self): return {}
        def declared_stability(self): return {m: "preserved" for m in g.MUTATIONS}
    assert isinstance(P(), g.NodeTypeProvider)
    class NotP:
        type_id = "acme.example"
    assert not isinstance(NotP(), g.NodeTypeProvider)
    assert g.TYPE_ID_RE.match("acme.example") and not g.TYPE_ID_RE.match("Acme") and not g.TYPE_ID_RE.match("nodot")


def test_observation_sort_key_is_deterministic():
    a, b = _obs(qualified_name="pkg.m.b"), _obs(qualified_name="pkg.m.a")
    assert [o.qualified_name for o in sorted([a, b], key=g.observation_key)] == ["pkg.m.a", "pkg.m.b"]
    assert g.observation_json(a) == g.observation_json(_obs(qualified_name="pkg.m.b"))
