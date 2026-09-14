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
