"""Arbiter calibration and evaluation (phase 7 Task 2). A calibration file is
an export of identity_ambiguous events (`analyzer ambiguities <db> --export`)
in which a person fills `truth`; `run()` replays an Arbiter over it N times
and writes a report — the bar an arbiter must clear before the analyzer lets
it write identity_asserted events:

  consistency == 1.0   the same answer every run (a model that wavers is out)
  accuracy   >= 0.9    on >= 10 labelled items
  evidence_ok          every assertion carries a non-empty evidence string
  sha                  the report was made on the calibration file in use

No model is wired here; `gate()` only reads a report. Values in the report
are counts over the arbiter's answers — nothing is judged by a model.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..graph_api import Ambiguity, Arbiter, Assertion, Row

VERSION = 1
MIN_TRUTH = 10
MIN_ACCURACY = 0.9
DEFAULT_EVAL_DIR = "~/skill-workspace/arbiter-eval"


def eval_dir() -> Path:
    return Path(os.environ.get("PROVLEDGER_ARBITER_EVAL_DIR") or os.path.expanduser(DEFAULT_EVAL_DIR))


def report_path(arbiter_id: str) -> Path:
    return eval_dir() / f"{arbiter_id}.json"


def file_sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_arbiter(spec: str) -> Arbiter:
    """'pkg.mod:Class' -> an instance that satisfies graph_api.Arbiter."""
    mod_name, sep, cls_name = spec.partition(":")
    if not sep or not mod_name or not cls_name:
        raise ValueError(f"arbiter spec must be pkg.mod:Class, got {spec!r}")
    obj = getattr(importlib.import_module(mod_name), cls_name)
    inst = obj() if isinstance(obj, type) else obj
    if not isinstance(inst, Arbiter):
        raise TypeError(f"{spec} is not an Arbiter (needs arbiter_id and arbitrate())")
    return inst


# ── the calibration file ──────────────────────────────────────────────────────

_ROW_FIELDS = ("qualified_name", "node_key", "node_type", "file_path", "line_start", "line_end",
               "struct_sig", "dataflow_sig")


def row_from_dict(d: dict, snapshot_id: int = 0) -> Row:  # phase 8: context (optional) rides along
    return Row(snapshot_id=snapshot_id, node_id=None, node_key=d.get("node_key") or "",
               node_type=d.get("node_type") or "function", qualified_name=d["qualified_name"],
               file_path=d.get("file_path"), line_start=d.get("line_start"), line_end=d.get("line_end"),
               struct_sig=d.get("struct_sig"), dataflow_sig=d.get("dataflow_sig"),
               dataflow_trivial=bool(d.get("dataflow_trivial", False)),
               owner_qn=d.get("owner_qn"), name=d.get("name"), context=d.get("context"))


def ambiguity_from_item(item: dict) -> Ambiguity:
    a = item["ambiguity"]
    prev = tuple(row_from_dict(r, i) for i, r in enumerate(a["prev"]))
    cur = tuple(row_from_dict(r, 1000 + i) for i, r in enumerate(a["cur"]))
    return Ambiguity(layer=a.get("layer") or "struct_sig", prev=prev, cur=cur)


def load_calibration(path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError(f"{path}: a calibration file is an object with an 'items' list")
    for i, it in enumerate(data["items"]):
        for key in ("id", "ambiguity"):
            if key not in it:
                raise ValueError(f"{path}: items[{i}] lacks {key!r}")
        t = it.get("truth")
        if not (t is None or t == "none" or (isinstance(t, dict) and isinstance(t.get("pairs"), list))):
            raise ValueError(f"{path}: items[{i}].truth must be null, \"none\" or {{\"pairs\": [[prev_qn, cur_qn], ...]}}")
    return data


# ── the report ─────────────────────────────────────────────────────────────────

@dataclass
class ArbiterReport:
    arbiter_id: str
    calibration: str
    sha: str
    n_runs: int
    n_items: int
    n_truth: int
    consistency: float
    coverage: float
    accuracy: float | None
    evidence_ok: bool
    created_at: str
    items: list[dict] = field(default_factory=list)
    version: int = VERSION

    @property
    def ok(self) -> bool:
        return gate_reason(self, self.sha) is None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, sort_keys=True, default=str)


def _answer(assertions: list[Assertion], prev_qn_by_key: dict[str, str]) -> list[tuple[str, str]]:
    """The arbiter's answer as sorted (prev_qn, cur_qn) pairs."""
    return sorted((prev_qn_by_key.get(s.chosen_prev_key, s.chosen_prev_key), s.cur_qualified_name) for s in assertions)


def _truth_pairs(truth) -> list[tuple[str, str]] | None:
    if truth is None:
        return None
    if truth == "none":
        return []
    return sorted((p, c) for p, c in truth["pairs"])


def run(arbiter: Arbiter, calib_path, n_runs: int = 3, out_dir=None) -> ArbiterReport:
    """Replay `arbiter` over every item n_runs times; write the report to
    <out_dir or eval_dir()>/<arbiter_id>.json and return it. Assertions with
    empty evidence are REJECTED (dropped from the answer, evidence_ok=False)."""
    data = load_calibration(calib_path)
    sha = file_sha(calib_path)
    items_out: list[dict] = []
    consistent = covered = correct = 0
    n_truth = 0
    evidence_ok = True
    for it in data["items"]:
        amb = ambiguity_from_item(it)
        prev_qn_by_key = {r.node_key: r.qualified_name for r in amb.prev}
        answers: list[list[tuple[str, str]]] = []
        for _ in range(max(1, n_runs)):
            kept: list[Assertion] = []
            for s in arbiter.arbitrate([amb]):
                if not (s.evidence or "").strip():
                    evidence_ok = False
                    continue
                kept.append(s)
            answers.append(_answer(kept, prev_qn_by_key))
        is_consistent = all(a == answers[0] for a in answers)
        consistent += is_consistent
        covered += bool(answers[0])
        truth = _truth_pairs(it.get("truth"))
        is_correct = None
        if truth is not None:
            n_truth += 1
            is_correct = is_consistent and answers[0] == truth
            correct += bool(is_correct)
        items_out.append({"id": it["id"], "answer": answers[0], "answers_distinct": len({tuple(a) for a in answers}),
                          "truth": truth, "correct": is_correct})
    n = len(data["items"])
    rep = ArbiterReport(arbiter_id=arbiter.arbiter_id, calibration=str(calib_path), sha=sha, n_runs=n_runs,
                        n_items=n, n_truth=n_truth,
                        consistency=(consistent / n) if n else 1.0, coverage=(covered / n) if n else 0.0,
                        accuracy=(correct / n_truth) if n_truth else None, evidence_ok=evidence_ok,
                        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), items=items_out)
    out = Path(out_dir) if out_dir else eval_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{arbiter.arbiter_id}.json").write_text(rep.to_json() + "\n", encoding="utf-8")
    return rep


def load_report(arbiter_id: str, out_dir=None) -> ArbiterReport | None:
    p = (Path(out_dir) if out_dir else eval_dir()) / f"{arbiter_id}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    d.pop("version", None)
    return ArbiterReport(**d)


def gate_reason(rep: ArbiterReport | None, calib_sha: str | None, *, min_truth: int = MIN_TRUTH,
                min_accuracy: float = MIN_ACCURACY) -> str | None:
    """None when the report clears the bar, else the refusal reason."""
    if rep is None:
        return "no evaluation report"
    if rep.consistency < 1.0:
        return f"consistency {rep.consistency:.3f} < 1.0"
    if not rep.evidence_ok:
        return "an assertion without evidence"
    if rep.n_truth < min_truth:
        return f"only {rep.n_truth} labelled item(s) (< {min_truth})"
    if rep.accuracy is None or rep.accuracy < min_accuracy:
        return f"accuracy {rep.accuracy if rep.accuracy is not None else 'n/a'} < {min_accuracy} on {rep.n_truth} labelled items"
    if calib_sha is None:
        return "calibration file not available to check the report's sha"
    if rep.sha != calib_sha:
        return f"report sha {rep.sha[:8]} != calibration file sha {calib_sha[:8]}"
    return None


def gate(arbiter_id: str, calib_path, out_dir=None) -> tuple[bool, str]:
    """(ok, detail) for wiring `arbiter_id` into the resolver."""
    rep = load_report(arbiter_id, out_dir)
    sha = file_sha(calib_path) if calib_path and Path(calib_path).exists() else None
    reason = gate_reason(rep, sha)
    if reason is None:
        return True, f"passed (consistency {rep.consistency:.2f}, accuracy {rep.accuracy:.2f} on {rep.n_truth}, sha {sha[:8]})"
    return False, f"refused: {reason}"


# ── generated BY CONSTRUCTION (phase 8 Task 1, FL-041) ────────────────────────
# A calibration item's truth has two legitimate sources: a person, or a change
# that was applied programmatically so the answer was known the moment it was
# made. Everything below is the second kind. The extractor is stdlib-only —
# struct_sig is the analyzer's own function (providers.builtin_symbols) and the
# dataflow signature is a light stand-in (the literal read targets of the
# body) — so the items are built without a graph DB; a model labels nothing.
import ast as _ast
import re as _re
import shutil as _shutil
import tempfile as _tempfile
from collections import Counter as _Counter

from ..providers import builtin_symbols as _bs
from . import harness as _harness

CONTEXT_LINES = 10
_DEFS = (_ast.FunctionDef, _ast.AsyncFunctionDef)


def _module_qn(rel: str) -> str:
    p = rel[:-3] if rel.endswith(".py") else rel
    p = p.replace("\\", "/")
    if p.endswith("/__init__"):
        p = p[:-9]
    return p.replace("/", ".")


def _read_targets(node) -> list[str]:
    """Literal first arguments of read_*() calls in the body — the light dataflow layer."""
    out: set[str] = set()
    for n in _ast.walk(node):
        if isinstance(n, _ast.Call):
            f = n.func
            name = f.attr if isinstance(f, _ast.Attribute) else (f.id if isinstance(f, _ast.Name) else "")
            if name.startswith("read_") and n.args and isinstance(n.args[0], _ast.Constant) and isinstance(n.args[0].value, str):
                out.add(n.args[0].value)
    return sorted(out)


def _context(lines: list[str], line_start: int, line_end: int | None) -> str:
    lo = max(1, line_start - CONTEXT_LINES)
    hi = min(len(lines), (line_end or line_start) + CONTEXT_LINES)
    return "\n".join(f"{n:5d}  {lines[n - 1]}" for n in range(lo, hi + 1))


def extract_rows(repo, *, snapshot_base: int = 0, keyed: bool = False) -> list[Row]:
    """Matcher rows of every function / method / class in `repo` (stdlib AST)."""
    repo = Path(repo)
    rows: list[Row] = []
    i = snapshot_base
    for py in sorted(repo.rglob("*.py")):
        rel = py.relative_to(repo).as_posix()
        if "__pycache__" in rel:
            continue
        src = py.read_text(encoding="utf-8")
        tree = _ast.parse(src)
        lines = src.splitlines()
        mod = _module_qn(rel)
        for node in tree.body:
            if isinstance(node, _DEFS):
                defs = [(node, "function", f"{mod}.{node.name}")]
            elif isinstance(node, _ast.ClassDef):
                defs = [(node, "class", f"{mod}.{node.name}")] + [
                    (m, "method", f"{mod}.{node.name}.{m.name}") for m in node.body if isinstance(m, _DEFS)]
            else:
                continue
            for d, ntype, qn in defs:
                i += 1
                reads = _read_targets(d)
                rows.append(Row(snapshot_id=i, node_id=None, node_key=f"nk_{_bs._h(qn)}" if keyed else "",
                                node_type=ntype, qualified_name=qn, file_path=rel,
                                line_start=d.lineno, line_end=d.end_lineno, struct_sig=_bs.struct_sig(d),
                                dataflow_sig=_bs._h(json.dumps({"reads": reads})) if reads else None,
                                dataflow_trivial=not reads, context=_context(lines, d.lineno, d.end_lineno)))
    return rows


def row_to_dict(r: Row) -> dict:
    return {"qualified_name": r.qualified_name, "node_key": r.node_key, "node_type": r.node_type,
            "file_path": r.file_path, "line_start": r.line_start, "line_end": r.line_end,
            "struct_sig": r.struct_sig, "dataflow_sig": r.dataflow_sig, "dataflow_trivial": r.dataflow_trivial,
            "context": r.context}


def truth_kind(item: dict) -> str:
    """unlabelled | none | pairs (every prev and cur paired) | partial (some side left out)."""
    t = item.get("truth")
    if t is None:
        return "unlabelled"
    if t == "none":
        return "none"
    a = item["ambiguity"]
    prev = {r["qualified_name"] for r in a["prev"]}
    cur = {r["qualified_name"] for r in a["cur"]}
    pairs = t.get("pairs") or []
    if not pairs:
        return "none"
    return "pairs" if {p for p, _ in pairs} == prev and {c for _, c in pairs} == cur else "partial"


def _item(item_id: str, source: str, amb: Ambiguity, truth) -> dict:
    rows = amb.prev + amb.cur
    return {"id": item_id, "source": source, "labelled_by": "construction", "truth": truth,
            "ambiguity": {"layer": amb.layer,
                          "same_struct_sig": len({r.struct_sig for r in rows}) == 1,
                          "same_dataflow_sig": len({r.dataflow_sig for r in rows}) == 1,
                          "prev": [row_to_dict(r) for r in amb.prev], "cur": [row_to_dict(r) for r in amb.cur]}}


def _ambiguities(before, after, matcher=None) -> list[Ambiguity]:
    from ..graph_api import match as _match
    prev = extract_rows(before, keyed=True)
    cur = extract_rows(after, snapshot_base=1000)
    return list((matcher or _match)(prev, cur).ambiguous)


def generate_from_corpus(corpus=None, *, matcher=None) -> list[dict]:
    """The hand-written `swap_two_similar` variants: truth is the `truth` table
    the variant's author declared in expect.toml (validated by the harness)."""
    items: list[dict] = []
    for case in _harness.iter_cases(corpus):
        spec = case.expect.get("variants", {}).get("swap_two_similar")
        vdir = case.variants.get("swap_two_similar")
        if not spec or vdir is None or not (vdir / "before").exists():
            continue
        truth = spec.get("truth")
        if not truth:
            raise ValueError(f"{case.name}: swap_two_similar declares no truth mapping")
        for k, amb in enumerate(_ambiguities(vdir / "before", vdir / "after", matcher)):
            prev_qns = [r.qualified_name for r in amb.prev]
            if not set(prev_qns) & set(truth):
                continue
            missing = [q for q in prev_qns if q not in truth]
            if missing:
                raise ValueError(f"{case.name}: truth does not cover {missing}")
            items.append(_item(f"corpus-{case.name}-{k}", f"corpus:{case.name}/swap_two_similar", amb,
                               {"pairs": [[q, truth[q]] for q in prev_qns]}))
    return items


# ── source surgery (line-based; the corpus is never executed) ─────────────────

def _hex(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:6]


def _locate(repo: Path, qn: str):
    """(file, src, def node, owning ClassDef | None) of a function / method qualified name."""
    for py in sorted(repo.rglob("*.py")):
        rel = py.relative_to(repo).as_posix()
        mod = _module_qn(rel)
        if not qn.startswith(mod + "."):
            continue
        rest = qn[len(mod) + 1:].split(".")
        src = py.read_text(encoding="utf-8")
        tree = _ast.parse(src)
        for node in tree.body:
            if len(rest) == 1 and isinstance(node, _DEFS) and node.name == rest[0]:
                return py, src, node, None
            if len(rest) == 2 and isinstance(node, _ast.ClassDef) and node.name == rest[0]:
                for m in node.body:
                    if isinstance(m, _DEFS) and m.name == rest[1]:
                        return py, src, m, node
    return None


def _def_text(src: str, node) -> str:
    return "\n".join(src.splitlines()[node.lineno - 1:node.end_lineno])


def _with_name(def_text: str, old: str, new: str) -> str:
    return _re.sub(rf"^(\s*(?:async\s+)?def\s+){_re.escape(old)}\b", rf"\g<1>{new}", def_text, count=1, flags=_re.M)


def _with_docstring(def_text: str, doc: str) -> str:
    """Replace (or insert) the def's docstring — the struct signature ignores it."""
    lines = def_text.splitlines()
    head = 0
    while not lines[head].rstrip().endswith(":"):
        head += 1
    body_indent = lines[head + 1][:len(lines[head + 1]) - len(lines[head + 1].lstrip())] if head + 1 < len(lines) else "    "
    node = _ast.parse("\n".join(l[len(lines[0]) - len(lines[0].lstrip()):] for l in lines)).body[0]
    first = node.body[0]
    if isinstance(first, _ast.Expr) and isinstance(first.value, _ast.Constant) and isinstance(first.value.value, str):
        del lines[head + 1:head + first.end_lineno]
    lines.insert(head + 1, f'{body_indent}"""{doc}"""')
    return "\n".join(lines)


def _insert_after(src: str, node, text: str) -> str:
    lines = src.splitlines()
    lines[node.end_lineno:node.end_lineno] = ["", *text.splitlines()]
    return "\n".join(lines) + "\n"


def _delete_def(src: str, node, owner) -> str:
    lines = src.splitlines()
    lo, hi = node.lineno - 1, node.end_lineno
    while hi < len(lines) and not lines[hi].strip():
        hi += 1
    del lines[lo:hi]
    if owner is not None and sum(1 for m in owner.body if isinstance(m, _DEFS) and m is not node) == 0:
        lines.insert(owner.lineno, "    pass")
    return "\n".join(lines) + "\n"


def _rename_symbol(src: str, old: str, new: str, *, method: bool) -> str:
    """Rename a def and every reference to it in one file: the def line, Name
    loads, `from m import old` aliases and — for methods — attribute accesses."""
    tree = _ast.parse(src)
    lines = src.splitlines()
    spans: list[tuple[int, int, int, str]] = []
    for node in _ast.walk(tree):
        if isinstance(node, _DEFS) and node.name == old:
            col = lines[node.lineno - 1].index(old, node.col_offset)
            spans.append((node.lineno, col, col + len(old), new))
        elif not method and isinstance(node, _ast.Name) and node.id == old:
            spans.append((node.lineno, node.col_offset, node.end_col_offset, new))
        elif not method and isinstance(node, _ast.alias) and node.name == old and node.lineno is not None:
            col = lines[node.lineno - 1].index(old, node.col_offset)
            spans.append((node.lineno, col, col + len(old), new))
        elif method and isinstance(node, _ast.Attribute) and node.attr == old and isinstance(node.value, _ast.Name):
            # `trainer.fit(...)` / `self.fit(...)` follow the rename; `self.model.fit(...)` is someone else's fit
            col = lines[node.end_lineno - 1].rfind(old, 0, node.end_col_offset)
            spans.append((node.end_lineno, col, col + len(old), new))
    for lineno, col, end_col, rep in sorted(spans, key=lambda s: (s[0], s[1]), reverse=True):
        line = lines[lineno - 1]
        lines[lineno - 1] = line[:col] + rep + line[end_col:]
    return "\n".join(lines) + "\n"


def _rename_everywhere(repo: Path, old: str, new: str, *, method: bool) -> None:
    for py in sorted(repo.rglob("*.py")):
        s = py.read_text(encoding="utf-8")
        if old in s:
            py.write_text(_rename_symbol(s, old, new, method=method), encoding="utf-8")


def _stranger_module(repo: Path, rel_dir: Path, hexid: str, defs: list[tuple[str, str]], *, method: bool) -> None:
    """A new module with unrelated definitions (same bodies as some F, on purpose)."""
    body = ['"""Ad-hoc helpers for a one-off report (new in this change)."""', "import pandas as pd", ""]
    if method:
        body += [f"class Zz{hexid}:", '    """A scratch container for one-off report helpers."""', ""]
        for name, text in defs:
            body += [_with_docstring(text, f"Scratch helper {name} for a one-off report; introduced new here."), ""]
    else:
        for name, text in defs:
            body += ["", _with_docstring(text, f"Scratch helper {name} for a one-off report; introduced new here."), ""]
    body += ["", f"def run_{hexid}(*args):", "    " + " and ".join(
        (f"Zz{hexid}().{n}(*args)" if method else f"{n}(*args)") for n, _ in defs), ""]
    (repo / rel_dir / f"zz_{hexid}.py").write_text("\n".join(body), encoding="utf-8")


def _eligible(case: _harness.Case) -> list[str]:
    """The case's function / method symbols (classes are matched through their methods)."""
    out = []
    for qn in case.expect["case"]["symbols"]:
        loc = _locate(case.base, qn)
        if loc is not None:
            out.append(qn)
    return out


def _copy(case_dir: Path, td: str, name: str) -> Path:
    dst = Path(td) / name
    _shutil.copytree(case_dir, dst)
    return dst


def _apply(repo: Path, qn: str, fn) -> None:
    py, src, node, owner = _locate(repo, qn)
    py.write_text(fn(src, node, owner), encoding="utf-8")


def _take(ambs: list[Ambiguity], layer: str, *qns: str) -> Ambiguity | None:
    for a in ambs:
        if a.layer == layer and {r.qualified_name for r in a.prev} >= set(qns):
            return a
    return None


def generate_swaps(corpus=None, *, matcher=None) -> list[dict]:
    """For every function F of every case: before = base + a body-identical
    twin F_legacy; after = both renamed (F -> F_x, F_legacy -> F_legacy_x) with
    every caller updated. The struct layer sees 2:2; the truth is the rename we
    applied. source="swap:<case>/<F>"."""
    items: list[dict] = []
    for case in _harness.iter_cases(corpus):
        for qn in _eligible(case):
            name = qn.rsplit(".", 1)[-1]
            method = _locate(case.base, qn)[3] is not None
            twin, twin_qn = f"{name}_legacy", f"{qn}_legacy"
            with _tempfile.TemporaryDirectory() as td:
                before = _copy(case.base, td, "before")
                _apply(before, qn, lambda s, n, o: _insert_after(
                    s, n, _with_docstring(_with_name(_def_text(s, n), name, twin), "Legacy copy kept for compatibility.")))
                after = _copy(before, td, "after")
                _rename_everywhere(after, twin, f"{twin}_x", method=method)
                _rename_everywhere(after, name, f"{name}_x", method=method)
                amb = _take(_ambiguities(before, after, matcher), "struct_sig", qn, twin_qn)
            if amb is None:
                continue
            items.append(_item(f"swap-{case.name}-{name}", f"swap:{case.name}/{name}", amb,
                               {"pairs": [[qn, f"{qn}_x"], [twin_qn, f"{twin_qn}_x"]]}))
    return items


def generate_negatives(corpus=None, *, matcher=None) -> list[dict]:
    """Ambiguities that must NOT be linked, built per function F of every case:

    * none    (2:2, struct layer): before = base + twin F_legacy; after = both
              deleted, two unrelated definitions with the same body appear in a
              new module (`zz_<hex>.py`, scratch docstrings, called by run_<hex>).
    * partial (1:2, struct layer): after = F renamed to F_x (callers updated)
              plus one unrelated same-body stranger in a new module → truth {F: F_x}.
    * and, once per case, the same two shapes on the DATAFLOW layer: helpers that
      read the same literal source with different bodies (none / partial).
    """
    items: list[dict] = []
    for case in _harness.iter_cases(corpus):
        for qn in _eligible(case):
            name = qn.rsplit(".", 1)[-1]
            loc = _locate(case.base, qn)
            method = loc[3] is not None
            rel_dir = Path(loc[0].relative_to(case.base)).parent
            hexid = _hex(case.name, qn)
            twin, twin_qn = f"{name}_legacy", f"{qn}_legacy"
            with _tempfile.TemporaryDirectory() as td:
                # none: F + F_legacy vanish; two strangers with the same body appear elsewhere
                before = _copy(case.base, td, "before")
                _apply(before, qn, lambda s, n, o: _insert_after(
                    s, n, _with_docstring(_with_name(_def_text(s, n), name, twin), "Legacy copy kept for compatibility.")))
                body = _def_text(*_locate(before, qn)[1:3])
                after = _copy(before, td, "after")
                _apply(after, twin_qn, lambda s, n, o: _delete_def(s, n, o))
                _apply(after, qn, lambda s, n, o: _delete_def(s, n, o))
                g1, g2 = f"zz_{hexid}_a", f"zz_{hexid}_b"
                _stranger_module(after, rel_dir, hexid, [(g1, _with_name(body, name, g1)), (g2, _with_name(body, name, g2))],
                                 method=method)
                amb = _take(_ambiguities(before, after, matcher), "struct_sig", qn, twin_qn)
                if amb is not None and len(amb.cur) == 2:
                    items.append(_item(f"negative-{case.name}-{name}-none", f"negative:{case.name}/{name}", amb, "none"))
                # partial: F renamed (callers follow) + one stranger with F's body
                after2 = _copy(case.base, td, "after2")
                _rename_everywhere(after2, name, f"{name}_x", method=method)
                g = f"zz_{hexid}_c"
                _stranger_module(after2, rel_dir, hexid, [(g, _with_name(body, name, g))], method=method)
                amb = _take(_ambiguities(case.base, after2, matcher), "struct_sig", qn)
                if amb is not None and len(amb.cur) == 2:
                    items.append(_item(f"negative-{case.name}-{name}-partial", f"negative:{case.name}/{name}", amb,
                                       {"pairs": [[qn, f"{qn}_x"]]}))
        items += _dataflow_negatives(case, matcher)
    return items


def _dataflow_negatives(case: _harness.Case, matcher=None) -> list[dict]:
    """Two helpers that read the same literal source (equal light dataflow
    signature) but do different things (different struct signature)."""
    qn = next(iter(_eligible(case)), None)
    if qn is None:
        return []
    loc = _locate(case.base, qn)
    rel = Path(loc[0].relative_to(case.base))
    mod = _module_qn(rel.as_posix())
    hexid = _hex(case.name, "dataflow")
    src_lit = f"data/{hexid}.csv"
    a_name, b_name = f"agg_{hexid}_by_region", f"agg_{hexid}_by_month"
    a = (f"def {a_name}():\n    \"\"\"Regional totals for the weekly report.\"\"\"\n"
         f"    df = pd.read_csv({src_lit!r})\n    return df.groupby('region').amount.sum()\n")
    b = (f"def {b_name}():\n    \"\"\"Monthly totals for the weekly report.\"\"\"\n"
         f"    df = pd.read_csv({src_lit!r})\n    df['month'] = df.ts.str[:7]\n    return df.groupby('month').amount.mean()\n")
    c = (f"def count_{hexid}():\n    \"\"\"Scratch: how many rows the source has today.\"\"\"\n"
         f"    return len(pd.read_csv({src_lit!r}))\n")
    d = (f"def head_{hexid}():\n    \"\"\"Scratch: the first rows of the source for eyeballing.\"\"\"\n"
         f"    return pd.read_csv({src_lit!r}).head(20)\n")
    items: list[dict] = []
    with _tempfile.TemporaryDirectory() as td:
        before = _copy(case.base, td, "before")
        f = before / rel
        f.write_text(f.read_text(encoding="utf-8").rstrip("\n") + "\n\n\n" + a + "\n\n" + b, encoding="utf-8")
        # none: both helpers gone, two scratch readers of the same source appear elsewhere
        after = _copy(case.base, td, "after")
        (after / rel.parent / f"zz_{hexid}.py").write_text(
            '"""Scratch readers for a one-off check (new in this change)."""\nimport pandas as pd\n\n\n' + c + "\n\n" + d,
            encoding="utf-8")
        amb = _take(_ambiguities(before, after, matcher), "dataflow_sig", f"{mod}.{a_name}", f"{mod}.{b_name}")
        if amb is not None:
            items.append(_item(f"negative-{case.name}-dataflow-none", f"negative:{case.name}/dataflow", amb, "none"))
        # partial: by_region kept (renamed, body slightly changed) + one scratch reader
        before2 = _copy(case.base, td, "before2")
        f2 = before2 / rel
        f2.write_text(f2.read_text(encoding="utf-8").rstrip("\n") + "\n\n\n" + a, encoding="utf-8")
        after2 = _copy(case.base, td, "after2")
        f3 = after2 / rel
        renamed = f"region_totals_{hexid}"
        f3.write_text(f3.read_text(encoding="utf-8").rstrip("\n") + "\n\n\n"
                      + a.replace(a_name, renamed).replace(".sum()\n", ".sum().reset_index()\n"), encoding="utf-8")
        (after2 / rel.parent / f"zz_{hexid}.py").write_text(
            '"""Scratch readers for a one-off check (new in this change)."""\nimport pandas as pd\n\n\n' + c, encoding="utf-8")
        amb = _take(_ambiguities(before2, after2, matcher), "dataflow_sig", f"{mod}.{a_name}")
        if amb is not None and len(amb.cur) == 2:
            items.append(_item(f"negative-{case.name}-dataflow-partial", f"negative:{case.name}/dataflow", amb,
                               {"pairs": [[f"{mod}.{a_name}", f"{mod}.{renamed}"]]}))
    return items


def _dedupe_key(item: dict, fields) -> tuple:
    a = item["ambiguity"]
    vals = {"layer": a.get("layer"), "prev_qns": tuple(sorted(r["qualified_name"] for r in a["prev"])),
            "cur_qns": tuple(sorted(r["qualified_name"] for r in a["cur"]))}
    return tuple(vals[f] for f in fields)


def merge(*item_lists, dedupe_by=("layer", "prev_qns", "cur_qns")) -> list[dict]:
    """Concatenate, dropping items that describe the same ambiguity; a labelled
    copy (truth not null) beats an unlabelled one, otherwise the first wins."""
    out: dict[tuple, dict] = {}
    order: list[tuple] = []
    for items in item_lists:
        for it in items:
            k = _dedupe_key(it, dedupe_by)
            if k not in out:
                out[k] = it
                order.append(k)
            elif out[k].get("truth") is None and it.get("truth") is not None:
                out[k] = it
    return [out[k] for k in order]


def stats(items) -> dict:
    """Counts by layer / source kind / truth kind / node type (of the prev rows)."""
    items = list(items)
    return {"n": len(items),
            "by_layer": dict(_Counter(it["ambiguity"].get("layer") or "?" for it in items)),
            "by_source": dict(_Counter((it.get("source") or "unknown").split(":", 1)[0] for it in items)),
            "by_truth": dict(_Counter(truth_kind(it) for it in items)),
            "by_node_type": dict(_Counter((it["ambiguity"]["prev"][0].get("node_type") or "?")
                                          for it in items if it["ambiguity"]["prev"]))}


def stats_line(s: dict) -> str:
    t = s["by_truth"]
    return (f"items={s['n']} pairs={t.get('pairs', 0)} none={t.get('none', 0)} partial={t.get('partial', 0)} "
            f"unlabelled={t.get('unlabelled', 0)} | "
            + " ".join(f"{k}={v}" for k, v in sorted(s["by_layer"].items())) + " | "
            + " ".join(f"{k}={v}" for k, v in sorted(s["by_source"].items())) + " | "
            + " ".join(f"{k}={v}" for k, v in sorted(s["by_node_type"].items())))
