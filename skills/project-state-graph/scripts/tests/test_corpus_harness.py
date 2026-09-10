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


# ── case loading + run_case ──────────────────────────────────────────────────
import pytest
import tomllib
from pathlib import Path
from tests.corpus.harness import load_case, run_case


def _oracle(repo: Path):
    d = tomllib.loads((repo / "oracle.toml").read_text())
    return [NodeObs(n.get("type", "function"), n["qn"], n.get("path", "m.py"), n.get("sig", "s"),
                    n.get("df", "d"), n.get("trivial", False)) for n in d.get("node", [])]


def _qn_matcher(prev, cur):
    pc = {o.qualified_name: o for o in cur}
    pairs, removed = [], []
    for o in prev:
        if o.qualified_name in pc:
            c = pc.pop(o.qualified_name)
            pairs.append(Pair(o, c, "qualname", tuple(f for f in ("struct_sig", "file_path") if getattr(o, f) != getattr(c, f))))
        else:
            removed.append(o)
    return MatchResult(pairs, removed, list(pc.values()))


def _case(tmp_path, variants: dict, expect_variants: str, base='[[node]]\nqn="m.f"\n'):
    c = tmp_path / "cases" / "demo"
    (c / "base").mkdir(parents=True)
    (c / "base" / "oracle.toml").write_text(base)
    for name, body in variants.items():
        if isinstance(body, dict):  # before/after
            for k, v in body.items():
                (c / "variants" / name / k).mkdir(parents=True)
                (c / "variants" / name / k / "oracle.toml").write_text(v)
        else:
            (c / "variants" / name).mkdir(parents=True)
            (c / "variants" / name / "oracle.toml").write_text(body)
    (c / "expect.toml").write_text(f'[case]\nname="demo"\nsymbols=["m.f"]\n[generated]\n[variants]\n{expect_variants}\n')
    return c


def test_run_case_ok(tmp_path):
    c = _case(tmp_path, {
        "same": '[[node]]\nqn="m.f"\n',
        "changed": '[[node]]\nqn="m.f"\nsig="s2"\n',
        "gone": '[[node]]\nqn="m.g"\n',
    }, 'same={identity="preserved", semantic_diff="none"}\n'
       'changed={identity="preserved", semantic_diff="expected"}\n'
       'gone={identity="broken", broken=["m.f"]}')
    assert run_case(load_case(c), _oracle, _qn_matcher) == []


def test_run_case_before_after(tmp_path):
    c = _case(tmp_path, {"ba": {"before": '[[node]]\nqn="m.f"\nsig="x"\n', "after": '[[node]]\nqn="m.f"\nsig="x"\n'}},
              'ba={identity="preserved", semantic_diff="none"}')
    assert run_case(load_case(c), _oracle, _qn_matcher) == []


def test_run_case_variant_symbols_override(tmp_path):
    """A variant may widen the symbol set (swap_two_similar needs the extra pair)."""
    c = _case(tmp_path, {"ba": {"before": '[[node]]\nqn="m.f"\n[[node]]\nqn="m.h"\n',
                                "after": '[[node]]\nqn="m.f"\n'}},
              'ba={identity="broken", broken=["m.h"], symbols=["m.f", "m.h"]}')
    assert run_case(load_case(c), _oracle, _qn_matcher) == []


def test_run_case_reports_mismatch(tmp_path):
    c = _case(tmp_path, {"gone": '[[node]]\nqn="m.g"\n'}, 'gone={identity="preserved", semantic_diff="none"}')
    fails = run_case(load_case(c), _oracle, _qn_matcher)
    assert fails and "gone" in fails[0]


def test_run_case_flags_nondeterministic_extractor(tmp_path):
    c = _case(tmp_path, {}, "")
    n = {"i": 0}

    def flaky(repo):
        n["i"] += 1
        return [NodeObs("function", f"m.f{n['i']}", "m.py", "s", "d", False)]
    assert any("determinism" in f for f in run_case(load_case(c), flaky, _qn_matcher))


def test_run_case_generated_uses_registered_mutator(tmp_path):
    c = tmp_path / "cases" / "gen"
    (c / "base").mkdir(parents=True)
    (c / "base" / "oracle.toml").write_text('[[node]]\nqn="m.f"\n')
    (c / "expect.toml").write_text('[case]\nname="gen"\nsymbols=["m.f"]\n'
                                   '[generated]\nresig={identity="preserved", semantic_diff="expected"}\n[variants]\n')

    def resig(src: Path, dst: Path):
        (dst / "oracle.toml").write_text('[[node]]\nqn="m.f"\nsig="s2"\n')
    assert run_case(load_case(c), _oracle, _qn_matcher, generated={"resig": resig}) == []
    assert any("no mutator registered" in f for f in run_case(load_case(c), _oracle, _qn_matcher))


def test_load_case_rejects_undeclared_variant_dir(tmp_path):
    c = _case(tmp_path, {"x": '[[node]]\nqn="m.f"\n'}, "")
    with pytest.raises(ValueError):
        load_case(c)


# ── review #35 A-group: ambiguous / broken bind to the declared symbols ──────

def test_ambiguous_requires_declared_symbols_on_prev_side():
    """An ambiguity elsewhere in the repo must not satisfy the expectation."""
    a, b = _obs("m.f"), _obs("m.g")
    unrelated = _res(ambiguous=[([_obs("m.x"), _obs("m.y")], [_obs("m.x2"), _obs("m.y2")])])
    fails = check(["m.f", "m.g"], [a, b], [], unrelated, {"identity": "ambiguous"})
    assert fails and any("m.f" in f for f in fails), fails


def test_broken_rejects_extra_removal_outside_symbols():
    """`removed` must equal expect.broken exactly — a stray non-symbol removal fails."""
    a, b, z = _obs("m.f"), _obs("m.g"), _obs("m.z")
    res = _res([Pair(b, b, "qualname", ())], removed=[a, z])
    fails = check(["m.f", "m.g"], [a, b, z], [b], res, {"identity": "broken", "broken": ["m.f"]})
    assert fails and any("m.z" in f for f in fails), fails


def test_broken_requires_non_broken_symbols_to_be_paired():
    """A symbol outside `broken` that is neither paired nor removed is a silent loss."""
    a, b = _obs("m.f"), _obs("m.g")
    res = _res(removed=[a])  # m.g vanished without a pair, a removal or an ambiguity
    fails = check(["m.f", "m.g"], [a, b], [], res, {"identity": "broken", "broken": ["m.f"]})
    assert fails and any("m.g" in f for f in fails), fails


def test_run_case_determinism_is_order_sensitive(tmp_path):
    """Spec: two extractions must be byte-identical, order included."""
    c = _case(tmp_path, {}, "", base='[[node]]\nqn="m.f"\n[[node]]\nqn="m.g"\n')
    n = {"i": 0}

    def reordering(repo):
        n["i"] += 1
        obs = _oracle(repo)
        return obs if n["i"] == 1 else obs[::-1]
    assert any("determinism" in f for f in run_case(load_case(c), reordering, _qn_matcher))
