"""provledger.testing.conformance — the six contracts a NodeTypeProvider must
honour (phase 6). The suite demands honesty, not stability: a provider may be
broken by a mutation as long as it says so."""
import time

import pytest

from orchestrator import graph_api as g
from orchestrator import providers
from orchestrator.testing import conformance, example_provider

SIX = ["determinism", "purity", "stability_matches_declaration", "schema", "failure_isolation", "performance_budget"]


def test_honest_example_provider_passes_all_six():
    rep = conformance.run(example_provider.ModuleProvider())
    assert [c["name"] for c in rep.checks] == SIX
    assert rep.ok is True, [c for c in rep.checks if not c["ok"]]
    assert all(c["ok"] for c in rep.checks)


def test_run_provider_isolates_an_exception():
    class Boom(example_provider.ModuleProvider):
        def extract(self, ctx):
            raise RuntimeError("kaboom")
    ctx = conformance.default_context(conformance.default_corpus_cases()[0].base)
    obs, degraded, elapsed = providers.run_provider(Boom(), ctx, timeout_s=5)
    assert obs == [] and "RuntimeError" in degraded and "kaboom" in degraded and elapsed >= 0


def test_run_provider_times_out_and_degrades():
    class Sleepy(example_provider.ModuleProvider):
        def extract(self, ctx):
            time.sleep(2)
            return super().extract(ctx)
    ctx = conformance.default_context(conformance.default_corpus_cases()[0].base)
    obs, degraded, elapsed = providers.run_provider(Sleepy(), ctx, timeout_s=0.2)
    assert obs == [] and "timeout" in degraded and elapsed < 1.5


def test_run_provider_rejects_schema_violations_whole():
    class Liar(example_provider.ModuleProvider):
        def extract(self, ctx):
            base = super().extract(ctx)
            first = base[0]
            bad = g.NodeObservation(**{**first.__dict__, "attrs": {**first.attrs, "tier": "asserted"}})
            return [bad] + base[1:]
    ctx = conformance.default_context(conformance.default_corpus_cases()[0].base)
    obs, degraded, _ = providers.run_provider(Liar(), ctx, timeout_s=5)
    assert obs == [] and "tier" in degraded and "forbidden" in degraded


def test_run_provider_rejects_a_foreign_type_id():
    class Impostor(example_provider.ModuleProvider):
        def extract(self, ctx):
            return [g.NodeObservation(**{**o.__dict__, "type_id": "acme.other"}) for o in super().extract(ctx)]
    ctx = conformance.default_context(conformance.default_corpus_cases()[0].base)
    obs, degraded, _ = providers.run_provider(Impostor(), ctx, timeout_s=5)
    assert obs == [] and "type_id" in degraded


def test_purity_check_catches_a_writer(tmp_path):
    class Writer(example_provider.ModuleProvider):
        def extract(self, ctx):
            (ctx.repo_root and __import__("pathlib").Path(ctx.repo_root) / "touched.txt").write_text("x")
            return super().extract(ctx)
    rep = conformance.run(Writer())
    chk = next(c for c in rep.checks if c["name"] == "purity")
    assert chk["ok"] is False and "touched.txt" in chk["detail"]


def test_declared_stability_must_cover_every_mutation():
    class Vague(example_provider.ModuleProvider):
        def declared_stability(self):
            return {"rename_function": "preserved"}
    rep = conformance.run(Vague())
    chk = next(c for c in rep.checks if c["name"] == "stability_matches_declaration")
    assert chk["ok"] is False and "delete_function" in chk["detail"]


def test_report_shape():
    rep = conformance.run(example_provider.ModuleProvider())
    for c in rep.checks:
        assert set(c) >= {"name", "ok", "severity", "detail"} and c["severity"] in ("error", "warning")
    assert isinstance(rep.text(), str) and "conformance" in rep.text().lower()


def test_vacuous_pass_is_flagged_as_a_warning():
    """A provider whose nodes never appear in the corpus proves nothing: the
    declaration check must say so (warning, not a silent PASS)."""
    class Nothing(example_provider.ModuleProvider):
        type_id = "acme.nothing"
        def extract(self, ctx):
            return []
    rep = conformance.run(Nothing())
    chk = next(c for c in rep.checks if c["name"] == "stability_matches_declaration")
    assert chk["ok"] is False and chk["severity"] == "warning"
    assert "no observation" in chk["detail"].lower() and "corpus=" in chk["detail"]
    assert rep.ok is True                                   # a warning never fails the report


# ── phase 7 Task 0: run_provider(isolate="subprocess") ────────────────────────────

def test_run_provider_subprocess_returns_the_same_observations():
    ctx = conformance.default_context(conformance.default_corpus_cases()[0].base)
    want, d0, _ = providers.run_provider(example_provider.ModuleProvider(), ctx, timeout_s=10)
    got, d1, elapsed = providers.run_provider(example_provider.ModuleProvider(), ctx, timeout_s=10, isolate="subprocess")
    assert d0 is None and d1 is None, (d0, d1)
    assert [g.observation_key(o) for o in got] == [g.observation_key(o) for o in want] and got
    assert elapsed >= 0


class _SleepForever(example_provider.ModuleProvider):
    def extract(self, ctx):
        time.sleep(60)
        return []


class _Crash(example_provider.ModuleProvider):
    def extract(self, ctx):
        raise ValueError("bad day")


def test_run_provider_subprocess_kills_a_sleeping_provider_within_budget():
    ctx = conformance.default_context(conformance.default_corpus_cases()[0].base)
    obs, degraded, elapsed = providers.run_provider(_SleepForever(), ctx, timeout_s=0.5, isolate="subprocess")
    assert obs == [] and "timeout" in degraded and "killed" in degraded, degraded
    assert elapsed < 3, elapsed


def test_run_provider_subprocess_isolates_an_exception():
    ctx = conformance.default_context(conformance.default_corpus_cases()[0].base)
    obs, degraded, _ = providers.run_provider(_Crash(), ctx, timeout_s=5, isolate="subprocess")
    assert obs == [] and "ValueError" in degraded and "bad day" in degraded


def test_run_provider_rejects_an_unknown_isolate_mode():
    ctx = conformance.default_context(conformance.default_corpus_cases()[0].base)
    with pytest.raises(ValueError, match="isolate"):
        providers.run_provider(example_provider.ModuleProvider(), ctx, isolate="container")
