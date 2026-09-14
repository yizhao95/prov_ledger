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


def row_from_dict(d: dict, snapshot_id: int = 0) -> Row:
    return Row(snapshot_id=snapshot_id, node_id=None, node_key=d.get("node_key") or "",
               node_type=d.get("node_type") or "function", qualified_name=d["qualified_name"],
               file_path=d.get("file_path"), line_start=d.get("line_start"), line_end=d.get("line_end"),
               struct_sig=d.get("struct_sig"), dataflow_sig=d.get("dataflow_sig"),
               dataflow_trivial=bool(d.get("dataflow_trivial", False)),
               owner_qn=d.get("owner_qn"), name=d.get("name"))


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
