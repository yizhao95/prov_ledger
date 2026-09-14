"""provledger.testing.conformance — the six contracts a NodeTypeProvider must
honour, checked over the mutation corpus (phase 6, v2 C.4):

  1. determinism      — extracting the same repository twice gives byte-identical observations
  2. purity           — extract() reads only: no SQL writes, no file changes under the repo
  3. stability_matches_declaration — what a mutation does to the provider's nodes (preserved /
                        broken / ambiguous, decided by the HOST matcher) equals what the provider
                        declares, on every corpus case where the mutation reaches the provider's
                        nodes at all (a case it leaves untouched is no evidence). The suite demands
                        honesty, not stability.
  4. schema           — namespaced type_id, attrs satisfy the provider's own schema (tier forbidden),
                        known or x- signature layers
  5. failure_isolation — a raising provider is degraded (empty), never propagated or partial
  6. performance_budget — extraction stays inside the time budget (warning)

Hosts with a real graph inject `context_factory` (an ExtractionContext with
the built graph) and `matcher`; the defaults are package-native: an empty
read-only graph plus the host matcher (graph_api.match) over the observations.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import graph_api as g
from ..providers import run_provider
from . import harness, mutate

SIX = ("determinism", "purity", "stability_matches_declaration", "schema", "failure_isolation", "performance_budget")
_WRITE_SQL = ("insert", "update", "delete", "create", "drop", "alter", "replace")


@dataclass
class Report:
    ok: bool
    checks: list[dict] = field(default_factory=list)

    def text(self) -> str:
        lines = [f"Conformance: {'PASS' if self.ok else 'FAIL'}"]
        for c in self.checks:
            mark = "OK  " if c["ok"] else ("WARN" if c["severity"] == "warning" else "XX  ")
            lines.append(f"  [{mark}] {c['name']}: {c['detail']}")
        return "\n".join(lines)


def default_corpus_cases(corpus: Path | None = None) -> list[harness.Case]:
    return harness.iter_cases(corpus)


def _file_map(repo: Path) -> dict[str, int]:
    files = sorted(str(p.relative_to(repo)).replace(os.sep, "/") for p in repo.rglob("*")
                   if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts)
    return {rel: i + 1 for i, rel in enumerate(files)}


def default_context(repo: Path, *, run_id: int = 1) -> g.ExtractionContext:
    """Package-native context: an EMPTY read-only graph (node/edge tables) and
    the repo's file map — enough for providers that read source."""
    repo = Path(repo)
    d = tempfile.mkdtemp(prefix="provledger-conformance-")
    db = os.path.join(d, "graph.db")
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT, qualified_name TEXT,
                           file_path TEXT, line_start INTEGER, line_end INTEGER, metadata_json TEXT, run_id INTEGER);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT, confidence TEXT, run_id INTEGER);
    """)
    c.commit()
    c.close()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    def node_rows(kinds: tuple[str, ...]) -> list:
        ph = ",".join("?" for _ in kinds) or "''"
        return conn.execute(f"SELECT n.* FROM node n JOIN node_type t ON t.id = n.node_type_id "
                            f"WHERE t.name IN ({ph}) ORDER BY n.id", tuple(kinds)).fetchall()

    return g.ExtractionContext(repo_root=str(repo), conn_ro=conn, run_id=run_id, file_map=_file_map(repo),
                               node_rows=node_rows)


def _rows(observations, offset: int, keyed: bool):
    out, back = [], {}
    for i, o in enumerate(sorted(observations, key=g.observation_key), start=offset):
        st, df = o.signature("struct"), o.signature("dataflow")
        out.append(g.Row(i, o.node_id, f"k{i}" if keyed else "", o.node_type, o.qualified_name, o.file_path,
                         o.line_start, o.line_end, st.value if st else None, df.value if df else None,
                         bool(df.trivial) if df else True, o.owner_qn, o.name))
        back[i] = o
    return out, back


def default_matcher(prev, cur) -> dict:
    """The host matcher over observations: {pairs, removed, added, ambiguous}
    expressed in observations (prev rows keyed, cur rows unkeyed)."""
    p_rows, p_back = _rows(prev, 1, True)
    c_rows, c_back = _rows(cur, len(p_rows) + 1, False)
    out = g.match(p_rows, c_rows)
    return {"pairs": [(p_back[p.prev.snapshot_id], c_back[p.cur.snapshot_id], p.via, p.changed) for p in out.pairs],
            "removed": [p_back[r.snapshot_id] for r in out.removed],
            "added": [c_back[r.snapshot_id] for r in out.added],
            "ambiguous": [([p_back[r.snapshot_id] for r in a.prev], [c_back[r.snapshot_id] for r in a.cur])
                          for a in out.ambiguous]}


def observed_stability(base, variant, matcher, base_all=None, variant_all=None) -> str:
    """What the mutation did to the provider's base nodes, by the host matcher.
    base_all / variant_all (optional) add companion providers' observations to
    the match — owned nodes inherit identity through their owners — while the
    verdict is judged over the provider's own `base` nodes only."""
    out = matcher(base_all if base_all is not None else base, variant_all if variant_all is not None else variant)
    removed = {g.observation_key(o) for o in out["removed"]}
    amb = {g.observation_key(o) for prev, _ in out["ambiguous"] for o in prev}
    keys = [g.observation_key(o) for o in base]
    if any(k in amb for k in keys):
        return "ambiguous"
    if any(k in removed for k in keys):
        return "broken"
    own = set(keys)
    touched = any(g.observation_key(p) in own and (changed or p.qualified_name != c.qualified_name)
                  for p, c, _via, changed in out["pairs"])
    return "preserved" if touched else "untouched"


def _snapshot(repo: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in repo.rglob("*") if p.is_file()}


def run(provider, *, corpus: Path | None = None, declared_stability: dict | None = None,
        timeout_s: float = 30.0, matcher: Callable | None = None,
        context_factory: Callable[[Path], g.ExtractionContext] | None = None,
        companions: list | None = None) -> Report:
    matcher = matcher or default_matcher
    factory = context_factory or default_context
    cases = default_corpus_cases(corpus)
    checks: list[dict] = []
    base_obs: dict[str, list] = {}
    base_all: dict[str, list] = {}
    elapsed_max = 0.0
    companions = list(companions or [])

    def extract(repo: Path):
        ctx = factory(repo)
        obs, degraded, elapsed = run_provider(provider, ctx, timeout_s=timeout_s)
        extra: list = []
        for c in companions:
            cobs, cdeg, _ = run_provider(c, ctx, timeout_s=timeout_s)
            extra.extend(cobs)
        return obs, degraded, elapsed, obs + extra

    # 1. determinism
    problems = []
    for case in cases:
        o1, d1, e1, a1 = extract(case.base)
        o2, d2, e2, _ = extract(case.base)
        elapsed_max = max(elapsed_max, e1, e2)
        if d1 or d2:
            problems.append(f"{case.name}: degraded ({d1 or d2})")
            base_obs[case.name] = []
            base_all[case.name] = []
            continue
        base_all[case.name] = a1
        j1 = [g.observation_json(o) for o in sorted(o1, key=g.observation_key)]
        j2 = [g.observation_json(o) for o in sorted(o2, key=g.observation_key)]
        if j1 != j2:
            problems.append(f"{case.name}: two extractions of base differ")
        base_obs[case.name] = o1
    checks.append({"name": "determinism", "ok": not problems, "severity": "error",
                   "detail": "; ".join(problems) if problems else
                   f"{len(cases)} base(s) extracted twice, byte-identical ({sum(len(v) for v in base_obs.values())} observations)"})

    # 2. purity
    problems = []
    for case in cases:
        ctx = factory(case.base)
        sql_log: list[str] = []
        ctx.conn_ro.set_trace_callback(sql_log.append)
        try:
            ctx.conn_ro.execute("CREATE TABLE _provledger_probe (x)")
            problems.append(f"{case.name}: conn_ro accepted a CREATE — the context is not read-only")
        except sqlite3.OperationalError:
            pass
        sql_log.clear()
        before = _snapshot(case.base)
        obs, degraded, _ = run_provider(provider, ctx, timeout_s=timeout_s)
        ctx.conn_ro.set_trace_callback(None)
        writes = [s for s in sql_log if s.strip().lower().split(" ", 1)[0] in _WRITE_SQL]
        if writes:
            problems.append(f"{case.name}: extract issued write SQL: {writes[:2]}")
        after = _snapshot(case.base)
        if before != after:
            changed = sorted(set(before) ^ set(after) | {p for p in before if p in after and before[p] != after[p]})
            problems.append(f"{case.name}: extract changed files under the repo: {[os.path.basename(p) for p in changed][:3]}")
            for p in set(after) - set(before):       # undo the provider's mess so later checks see the corpus
                try:
                    os.remove(p)
                except OSError:
                    pass
    checks.append({"name": "purity", "ok": not problems, "severity": "error",
                   "detail": "; ".join(problems) if problems else "read-only graph, no write SQL, repository untouched"})

    # 3. stability matches declaration
    declared = dict(declared_stability if declared_stability is not None else provider.declared_stability())
    problems = []
    missing = [m for m in g.MUTATIONS if m not in declared]
    if missing:
        problems.append(f"declared_stability lacks {missing}")
    bad = {m: v for m, v in declared.items() if v not in g.STABILITY}
    if bad:
        problems.append(f"declared_stability has unknown values {bad} (valid: {list(g.STABILITY)})")
    exercised: set[str] = set()
    for case in cases:
        base = base_obs.get(case.name) or []
        for name in case.expect.get("generated", {}):
            if name not in g.MUTATIONS or name in missing or name not in mutate.GENERATED:
                continue
            with tempfile.TemporaryDirectory() as td:
                dst = Path(td) / "repo"
                shutil.copytree(case.base, dst)
                mutate.GENERATED[name](case.base, dst)
                v, dv, _, va = extract(dst)
            if dv:
                problems.append(f"{case.name}/{name}: variant extraction degraded ({dv})")
                continue
            observed = observed_stability(base, v, matcher, base_all.get(case.name), va)
            if observed == "untouched":
                continue                       # the mutation never reached this provider's nodes: no evidence
            exercised.add(name)
            if observed != declared[name]:
                problems.append(f"{case.name}/{name}: declared {declared[name]}, observed {observed}")
        for name, vdir in case.variants.items():
            if name not in g.MUTATIONS or name in missing:
                continue
            if (vdir / "before").exists():
                b, db_, _, ba = extract(vdir / "before")
                v, dv, _, va = extract(vdir / "after")
            else:
                b, db_, ba = base, None, base_all.get(case.name)
                v, dv, _, va = extract(vdir)
            if db_ or dv:
                problems.append(f"{case.name}/{name}: variant extraction degraded ({db_ or dv})")
                continue
            observed = observed_stability(b, v, matcher, ba, va)
            if observed == "untouched":
                continue
            exercised.add(name)
            if observed != declared[name]:
                problems.append(f"{case.name}/{name}: declared {declared[name]}, observed {observed}")
    checks.append({"name": "stability_matches_declaration", "ok": not problems, "severity": "error",
                   "detail": "; ".join(problems) if problems else
                   f"declaration holds for {sorted(exercised)} over {len(cases)} case(s)"})

    # 4. schema
    problems = []
    if not g.TYPE_ID_RE.match(getattr(provider, "type_id", "") or ""):
        problems.append(f"type_id {getattr(provider, 'type_id', None)!r} is not vendor.name")
    from ..providers import check_observations
    for case in cases:
        for p in check_observations(provider, base_obs.get(case.name) or []):
            problems.append(f"{case.name}: {p}")
    checks.append({"name": "schema", "ok": not problems, "severity": "error",
                   "detail": "; ".join(problems[:5]) if problems else "type_id namespaced, attrs valid, layers known"})

    # 5. failure isolation
    class _Raising:
        type_id = provider.type_id
        schema_version = getattr(provider, "schema_version", 1)
        requires = getattr(provider, "requires", ())

        def extract(self, ctx):
            partial = list(provider.extract(ctx))[:1]
            if partial:
                yield partial[0]
            raise RuntimeError("conformance: injected failure after a partial result")

        def attributes_schema(self):
            return provider.attributes_schema()

        def declared_stability(self):
            return provider.declared_stability()

    problems = []
    if cases:
        try:
            obs, degraded, _ = run_provider(_Raising(), factory(cases[0].base), timeout_s=timeout_s)
            if degraded is None or obs:
                problems.append(f"injected failure was not isolated (degraded={degraded!r}, observations={len(obs)})")
        except Exception as e:  # noqa: BLE001
            problems.append(f"injected failure propagated: {type(e).__name__}: {e}")
    checks.append({"name": "failure_isolation", "ok": not problems, "severity": "error",
                   "detail": "; ".join(problems) if problems else "an injected exception degrades the provider (empty), nothing propagates"})

    # 6. performance budget
    over = elapsed_max > timeout_s
    checks.append({"name": "performance_budget", "ok": not over, "severity": "warning",
                   "detail": f"slowest base extraction {elapsed_max:.2f}s vs budget {timeout_s}s"
                             + (" — over budget (would be degraded at run time)" if over else "")})

    ok = all(c["ok"] for c in checks if c["severity"] == "error")
    return Report(ok=ok, checks=checks)
