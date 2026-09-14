"""Thin shell: the corpus harness lives in the package (provledger.testing.harness,
phase 6); this module re-exports it for the analyzer tests and keeps the two
adapters that reach into analyzer.* (the package cannot import the analyzer)."""
from __future__ import annotations

import importlib as _importlib
import tempfile
from pathlib import Path

from analyzer._host import testing as _testing

_harness = _importlib.import_module(f"{_testing.__name__}.harness")
globals().update({k: v for k, v in vars(_harness).items() if not k.startswith("__")})
default_corpus = _harness.default_corpus


# ── adapters to the real analyzer (Task 12) ──────────────────────────────────
# Imported lazily: the harness itself stays stdlib-only; only these two
# functions reach into analyzer.* (signatures + history.match).

def analyzer_extractor(repo: Path) -> list[NodeObs]:
    """Build a throw-away graph of `repo` and return this run's snapshot rows of
    the identity-bearing symbols (function / method / class) as NodeObs."""
    from analyzer import (api_refs, data_model, dataflow, dataflow_types, history, py_ast,
                          sql_refs, store, walker)
    with tempfile.TemporaryDirectory() as td:
        conn = store.init_db(str(Path(td) / "corpus-state-graph.db"))
        run_id = store.start_run(conn, project_name="corpus")
        fm = walker.walk(conn, str(repo))
        py_ast.analyze(conn, str(repo), fm)
        dataflow.analyze(conn, str(repo), fm)
        dataflow_types.analyze(conn, str(repo), fm)
        data_model.analyze(conn, str(repo), fm)
        sql_refs.analyze(conn, str(repo), fm)
        api_refs.analyze(conn, str(repo), fm)
        history.snapshot_run(conn, str(repo), run_id)
        rows = history._rows(conn, run_id)
        conn.close()
    return [NodeObs(r.node_type, r.qualified_name, r.file_path, r.struct_sig, r.dataflow_sig, r.dataflow_trivial)
            for r in rows if r.node_type in history.SYMBOL_TYPES]


def analyzer_matcher(prev: list[NodeObs], cur: list[NodeObs]) -> MatchResult:
    """history.match over NodeObs: prev rows get synthetic keys, cur rows none."""
    from analyzer import history

    def rows(obs: list[NodeObs], offset: int, keyed: bool) -> tuple[list, dict]:
        out, back = [], {}
        for i, o in enumerate(sorted(obs), start=offset):
            r = history.Row(i, None, f"k{i}" if keyed else "", o.node_type, o.qualified_name, o.file_path,
                            None, None, o.struct_sig, o.dataflow_sig, o.dataflow_trivial)
            out.append(r)
            back[i] = o
        return out, back
    p_rows, p_back = rows(prev, 1, True)
    c_rows, c_back = rows(cur, len(p_rows) + 1, False)
    out = history.match(p_rows, c_rows)
    return MatchResult(
        [Pair(p_back[p.prev.snapshot_id], c_back[p.cur.snapshot_id], p.via, p.changed) for p in out.pairs],
        [p_back[r.snapshot_id] for r in out.removed],
        [c_back[r.snapshot_id] for r in out.added],
        [([p_back[r.snapshot_id] for r in a.prev], [c_back[r.snapshot_id] for r in a.cur]) for a in out.ambiguous],
    )
