"""orchestrator.extensions — explicit, namespaced, prioritised registration
from provledger-extensions.json (phase 5, FR-3 first layer)."""
import hashlib
import json

import pytest

from orchestrator import extensions as ext

GOOD = {
    "version": 1,
    "drift_kinds": [
        {"id": "acme.null_spike_strict", "metric": "null_frac", "op": "delta_gte", "value": 0.1, "priority": 10},
        {"id": "acme.rows_halved", "metric": "row_count", "op": "dropped_below", "value": 0.5},
    ],
    "namesets": [{"set": "split_funcs", "add": ["stratified_split", "time_split"]},
                 {"set": "fit_methods", "add": ["fit_transform_all"], "remove": ["train"]}],
    "constraints": [{"project": "prov_ledger", "statement": "orders.region != 'X' must stay excluded",
                     "subjects": ["pkg.pipeline.clean", "orders.region"], "keywords": ["scope-change"],
                     "why_ref": "https://wiki/decisions/42", "why_visibility": "restricted"}],
}


def _write(path, obj):
    path.write_text(json.dumps(obj))
    return str(path)


# ── discovery: three places, first hit wins, never merged ─────────────────────

def test_discover_prefers_repo_then_env_then_home(tmp_path, monkeypatch):
    repo = tmp_path / "repo"; repo.mkdir()
    home = tmp_path / "home"; (home / "skill-workspace").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PROVLEDGER_EXTENSIONS", raising=False)
    assert ext.discover(str(repo)) is None
    home_file = _write(home / "skill-workspace" / "provledger-extensions.json", GOOD)
    assert ext.discover(str(repo)) == home_file
    env_file = _write(tmp_path / "env.json", GOOD)
    monkeypatch.setenv("PROVLEDGER_EXTENSIONS", env_file)
    assert ext.discover(str(repo)) == env_file
    repo_file = _write(repo / "provledger-extensions.json", GOOD)
    assert ext.discover(str(repo)) == repo_file
    assert ext.discover(None) == env_file                     # no repo: env, then home


def test_load_none_is_empty():
    e = ext.load(None)
    assert e is ext.EMPTY and e.path is None and e.sha256 is None
    assert e.drift_kinds == () and e.namesets == () and e.constraints == ()
    assert e.fingerprint() is None


def test_load_good_file(tmp_path):
    p = _write(tmp_path / "x.json", GOOD)
    e = ext.load(p)
    assert e.path == p and e.sha256 == hashlib.sha256(open(p, "rb").read()).hexdigest()
    assert [k.id for k in e.drift_kinds] == ["acme.null_spike_strict", "acme.rows_halved"]
    assert e.drift_kinds[1].priority == 0 and e.drift_kinds[0].enabled is True
    assert e.namesets[1].remove == ("train",) and e.namesets[0].priority == 0
    c = e.constraints[0]
    assert c.project == "prov_ledger" and c.why_visibility == "restricted" and c.subjects == ("pkg.pipeline.clean", "orders.region")
    fp = e.fingerprint()
    assert fp == {"path": p, "sha256": e.sha256, "drift_kinds": ["acme.null_spike_strict", "acme.rows_halved"],
                  "namesets": {"split_funcs": ["stratified_split", "time_split"], "fit_methods": ["fit_transform_all", "-train"]},
                  "constraints": 1, "providers": []}
    assert ext.load(p).fingerprint() == fp                   # stable across loads


@pytest.mark.parametrize("patch, msg", [
    ({"drift_kinds": [{"id": "NoDot", "metric": "null_frac", "op": "eq", "value": 1}]}, "vendor.name"),
    ({"drift_kinds": [{"id": "acme.x", "metric": "null_frac", "op": "bogus", "value": 1}]}, "op"),
    ({"drift_kinds": [{"id": "acme.x", "metric": "colour", "op": "eq", "value": 1}]}, "metric"),
    ({"drift_kinds": [{"id": "acme.x", "metric": "null_frac", "op": "eq", "value": 1},
                      {"id": "acme.x", "metric": "row_count", "op": "eq", "value": 2}]}, "duplicate"),
    ({"drift_kinds": [{"id": "acme.a", "metric": "null_frac", "op": "eq", "value": 1, "priority": 3},
                      {"id": "acme.b", "metric": "null_frac", "op": "eq", "value": 2, "priority": 3}]}, "priority"),
    ({"drift_kinds": [{"id": "acme.x", "metric": "null_frac", "op": "eq", "value": 1, "priority": "high"}]}, "priority"),
    ({"namesets": [{"set": "split_funcs", "add": "not-a-list"}]}, "add"),
    ({"constraints": [{"statement": "s", "subjects": ["a"], "why_visibility": "secret"}]}, "why_visibility"),
    ({"constraints": [{"subjects": ["a"]}]}, "statement"),
    ({"version": 2}, "version"),
])
def test_load_rejects_bad_files(tmp_path, patch, msg):
    bad = {**{"version": 1}, **patch}
    p = _write(tmp_path / "bad.json", bad)
    with pytest.raises(ext.ExtensionsError) as e:
        ext.load(p)
    assert msg in str(e.value)


def test_load_rejects_unreadable_json(tmp_path):
    p = tmp_path / "x.json"; p.write_text("{not json")
    with pytest.raises(ext.ExtensionsError):
        ext.load(str(p))
