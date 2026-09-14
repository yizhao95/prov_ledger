"""LLM identity arbitration — consistency placeholder (phase 4 Task 5).

The identity_ambiguous scenario leaves two body-identical helpers unlinked
(identity_ambiguous on the struct_sig layer). Linking them is an arbitration
the deterministic matcher refuses to make; a model may. This test calls an
injected `arbitrate` N times on the ambiguity the scenario produces and
demands the SAME answer every time — the consistency bar a real arbiter must
clear before it is trusted with identity_asserted events.

By default the arbiter is a deterministic stub, so the test proves the
harness, not a model. It is marked `llm_consistency` and DESELECTED by
default (pyproject addopts); run it on purpose:

    python3 -m pytest tests/test_llm_consistency.py -m llm_consistency

Plugging in a real model: set PROVLEDGER_ARBITER="module:function" to a
callable with history.Arbitrate's signature (list[Ambiguity] -> list[Assertion]);
every Assertion must carry non-empty evidence. Never in CI.
"""
from __future__ import annotations

import importlib
import os

import pytest

from analyzer import history

N_RUNS = 3

# The ambiguity identity_ambiguous/golden.json records (prev/cur names).
PREV = ("pkg.pipeline.norm_a", "pkg.pipeline.norm_b")
CUR = ("pkg.pipeline.scale_a", "pkg.pipeline.scale_b")


def _row(qn: str, key: str) -> history.Row:
    fields = {f: None for f in history.Row.__dataclass_fields__}
    fields.update(snapshot_id=0, node_id=None, node_key=key, node_type="function", qualified_name=qn,
                  file_path="pkg/pipeline.py", line_start=1, line_end=2, struct_sig="same", dataflow_sig="same")
    return history.Row(**fields)


def stub_arbiter(ambiguities: list[history.Ambiguity]) -> list[history.Assertion]:
    """Deterministic placeholder: pair prev/cur by the suffix after the last '_'."""
    out = []
    for a in ambiguities:
        by_suffix = {r.qualified_name.rsplit("_", 1)[-1]: r for r in a.prev}
        for c in a.cur:
            p = by_suffix.get(c.qualified_name.rsplit("_", 1)[-1])
            if p is not None:
                out.append(history.Assertion(cur_qualified_name=c.qualified_name, chosen_prev_key=p.node_key,
                                             evidence=f"stub: shared suffix {c.qualified_name.rsplit('_', 1)[-1]!r}",
                                             arbiter="stub"))
    return out


def load_arbiter() -> history.Arbitrate:
    spec = os.environ.get("PROVLEDGER_ARBITER")
    if not spec:
        return stub_arbiter
    mod, fn = spec.split(":")
    return getattr(importlib.import_module(mod), fn)


class _CallableArbiter:
    """Wrap a bare `arbitrate` callable (PROVLEDGER_ARBITER=module:function) as a graph_api.Arbiter."""

    def __init__(self, fn, arbiter_id: str):
        self._fn, self.arbiter_id = fn, arbiter_id

    def arbitrate(self, ambiguities):
        return self._fn(ambiguities)


@pytest.mark.llm_consistency
def test_arbiter_answers_identically_n_times(tmp_path):
    """Phase 7: the same bar the analyzer's gate applies — calibration.run over
    the scenario's ambiguity, N_RUNS times: consistency must be 1.0, every
    assertion needs evidence, and every answer must stay inside the ambiguity."""
    import json
    from analyzer._host import testing as _testing
    cal = _testing.calibration
    row = lambda qn, key: {"qualified_name": qn, "node_key": key, "node_type": "function", "file_path": "pkg/pipeline.py",
                           "line_start": 1, "line_end": 2, "struct_sig": "same", "dataflow_sig": "same"}
    calib = tmp_path / "calib.json"
    calib.write_text(json.dumps({"version": 1, "items": [{"id": "scenario:identity_ambiguous", "truth": None, "labelled_by": None,
                                                           "ambiguity": {"layer": "struct_sig",
                                                                         "prev": [row(q, f"nk_{q.rsplit('.', 1)[-1]}") for q in PREV],
                                                                         "cur": [row(q, "") for q in CUR]}}]}))
    fn = load_arbiter()
    arbiter = _CallableArbiter(fn, os.environ.get("PROVLEDGER_ARBITER") or "stub")
    rep = cal.run(arbiter, calib, n_runs=N_RUNS, out_dir=tmp_path / "eval")
    assert rep.consistency == 1.0, f"inconsistent arbitration across {N_RUNS} runs: {rep.items}"
    assert rep.evidence_ok, "every assertion needs evidence"
    assert rep.coverage == 1.0, "the arbiter returned nothing"
    for prev_qn, cur_qn in rep.items[0]["answer"]:
        assert cur_qn in CUR and prev_qn in PREV
