"""providers.load_providers — explicit registration of third-party
NodeTypeProviders from provledger-extensions.json (phase 6 Task 4): built-ins
always (unless disabled by id), declared ones by import path in priority
order, every failure a degradation record instead of an exception."""
import json
import textwrap

import pytest

from orchestrator import extensions as ext
from orchestrator import providers

GOOD_MOD = '''
from provledger.graph_api import MUTATIONS, NodeObservation, Signature

class Example:
    type_id = "acme.example"
    schema_version = 2
    requires = ()
    def extract(self, ctx):
        return []
    def attributes_schema(self):
        return {}
    def declared_stability(self):
        return {m: "preserved" for m in MUTATIONS}

class NeedsLLM(Example):
    type_id = "acme.needs_llm"
    requires = ("llm",)

class NotAProvider:
    type_id = "acme.not_a_provider"
'''


@pytest.fixture
def acme(tmp_path, monkeypatch):
    (tmp_path / "acme_mod.py").write_text(textwrap.dedent(GOOD_MOD))
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


def _ext(tmp_path, providers_list):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"version": 1, "providers": providers_list}))
    return ext.load(str(p))


def _ids(plist):
    return [p.type_id for p in plist]


def test_builtins_by_default():
    plist, records = providers.load_providers(ext.EMPTY)
    assert _ids(plist) == ["provledger.symbol", "provledger.owned", "provledger.declared"]   # DP 2c added the third
    assert [r["id"] for r in records] == ["provledger.symbol", "provledger.owned", "provledger.declared"]
    assert all(r["degraded"] is None and r["enabled"] is True and r["priority"] == 0 for r in records)
    assert providers.load_providers(ext.EMPTY, builtin=False)[0] == []


def test_third_party_loaded_by_module_path_in_priority_order(acme):
    e = _ext(acme, [{"id": "acme.example", "module": "acme_mod:Example", "priority": 5}])
    plist, records = providers.load_providers(e)
    assert _ids(plist) == ["acme.example", "provledger.symbol", "provledger.owned", "provledger.declared"]      # priority 5 first
    rec = next(r for r in records if r["id"] == "acme.example")
    assert rec["module"] == "acme_mod:Example" and rec["schema_version"] == 2 and rec["degraded"] is None
    assert e.fingerprint()["providers"] == ["acme.example"]


def test_bad_module_degrades_instead_of_raising(acme):
    e = _ext(acme, [{"id": "acme.missing", "module": "nope_mod:Thing"}])
    plist, records = providers.load_providers(e)
    assert "acme.missing" not in _ids(plist)
    rec = next(r for r in records if r["id"] == "acme.missing")
    assert rec["degraded"] and "import" in rec["degraded"].lower()


def test_class_that_is_not_a_provider_degrades(acme):
    e = _ext(acme, [{"id": "acme.not_a_provider", "module": "acme_mod:NotAProvider"}])
    plist, records = providers.load_providers(e)
    assert "acme.not_a_provider" not in _ids(plist)
    assert "NodeTypeProvider" in next(r for r in records if r["id"] == "acme.not_a_provider")["degraded"]


def test_declared_id_must_match_the_class(acme):
    e = _ext(acme, [{"id": "acme.other", "module": "acme_mod:Example"}])
    plist, records = providers.load_providers(e)
    assert "acme.other" not in _ids(plist) and "acme.example" not in _ids(plist)
    assert "type_id" in next(r for r in records if r["id"] == "acme.other")["degraded"]


def test_unavailable_capability_degrades(acme):
    e = _ext(acme, [{"id": "acme.needs_llm", "module": "acme_mod:NeedsLLM"}])
    plist, records = providers.load_providers(e)
    assert "acme.needs_llm" not in _ids(plist)
    rec = next(r for r in records if r["id"] == "acme.needs_llm")
    assert "capability" in rec["degraded"] and "llm" in rec["degraded"]
    assert providers.HOST_CAPABILITIES == ()


def test_builtin_disabled_by_id(tmp_path):
    e = _ext(tmp_path, [{"id": "provledger.owned", "enabled": False}])
    plist, records = providers.load_providers(e)
    assert _ids(plist) == ["provledger.symbol", "provledger.declared"]
    rec = next(r for r in records if r["id"] == "provledger.owned")
    assert rec["enabled"] is False and rec["degraded"] is None


def test_disabled_third_party_is_recorded_not_loaded(acme):
    e = _ext(acme, [{"id": "acme.example", "module": "acme_mod:Example", "enabled": False}])
    plist, records = providers.load_providers(e)
    assert "acme.example" not in _ids(plist)
    assert next(r for r in records if r["id"] == "acme.example")["enabled"] is False


@pytest.mark.parametrize("decl, msg", [
    ({"id": "NoDot", "module": "m:C"}, "vendor.name"),
    ({"id": "acme.x", "module": "not-a-path"}, "module"),
    ({"id": "acme.x"}, "module"),
    ({"id": "acme.x", "module": "m:C", "timeout_s": "slow"}, "timeout_s"),
    ({"id": "acme.x", "module": "m:C", "priority": 1.5}, "priority"),
])
def test_providers_validation(tmp_path, decl, msg):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"version": 1, "providers": [decl]}))
    with pytest.raises(ext.ExtensionsError) as e:
        ext.load(str(p))
    assert msg in str(e.value)


def test_duplicate_provider_id_fails_the_load(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"version": 1, "providers": [{"id": "acme.x", "module": "m:A"}, {"id": "acme.x", "module": "m:B"}]}))
    with pytest.raises(ext.ExtensionsError) as e:
        ext.load(str(p))
    assert "duplicate" in str(e.value)


def test_provider_decl_defaults(tmp_path):
    e = _ext(tmp_path, [{"id": "acme.x", "module": "m:C"}])
    d = e.providers[0]
    assert (d.enabled, d.priority, d.timeout_s) == (True, 0, 30.0)
