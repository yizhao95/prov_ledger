"""Corpus harness core: check() enforces expect.toml semantics + built-in must_not."""
from tests.corpus.harness import NodeObs, Pair, MatchResult, check


def _obs(qn, sig="s1", df="d1", path="pkg/m.py", trivial=False, t="function"):
    return NodeObs(t, qn, path, sig, df, trivial)


def _res(pairs=(), removed=(), added=(), ambiguous=()):
    return MatchResult(list(pairs), list(removed), list(added), list(ambiguous))


def test_preserved_none_passes():
    a, b = _obs("m.f"), _obs("m.f")
    assert check(["m.f"], [a], [b], _res([Pair(a, b, "qualname", ())]),
                 {"identity": "preserved", "semantic_diff": "none"}) == []


def test_preserved_forbids_removed():
    a = _obs("m.f")
    fails = check(["m.f"], [a], [], _res(removed=[a]), {"identity": "preserved", "semantic_diff": "none"})
    assert any("node_removed" in f for f in fails)


def test_none_forbids_changed():
    a, b = _obs("m.f"), _obs("m.f", sig="s2")
    fails = check(["m.f"], [a], [b], _res([Pair(a, b, "qualname", ("struct_sig",))]),
                  {"identity": "preserved", "semantic_diff": "none"})
    assert any("node_changed" in f for f in fails)


def test_callers_allows_only_non_target_changes():
    f, g = _obs("m.f"), _obs("m.g")
    f2, g2 = _obs("m.run", sig="s1"), _obs("m.g", sig="s9")
    res = _res([Pair(f, f2, "struct_sig", ("qualified_name",)), Pair(g, g2, "qualname", ("struct_sig",))])
    assert check(["m.f", "m.g"], [f, g], [f2, g2], res,
                 {"identity": "preserved", "semantic_diff": "callers", "renamed": ["m.f"]}) == []


def test_broken_requires_exact_removed_set():
    a, b = _obs("m.f"), _obs("m.g")
    res = _res([Pair(b, b, "qualname", ())], removed=[a])
    assert check(["m.f", "m.g"], [a, b], [b], res, {"identity": "broken", "broken": ["m.f"]}) == []
    assert check(["m.f", "m.g"], [a, b], [b], res, {"identity": "broken", "broken": ["m.g"]}) != []


def test_ambiguous_expected():
    a, b = _obs("m.f"), _obs("m.g")
    res = _res(ambiguous=[([a, b], [_obs("m.g2"), _obs("m.f2")])])
    assert check(["m.f", "m.g"], [a, b], [], res, {"identity": "ambiguous"}) == []


def test_via_is_enforced_when_declared():
    a, b = _obs("m.f"), _obs("n.f", path="pkg/n.py")
    res = _res([Pair(a, b, "qualname", ())])
    assert check(["m.f"], [a], [b], res, {"identity": "preserved", "semantic_diff": "none", "via": "struct_sig"}) != []
