"""HeuristicArbiter — the baseline (and the worked example) of an Arbiter:
links an ambiguous current node to the previous node whose NAME is closest
by edit distance, only when that minimum is unique and the struct and
dataflow signatures are equal. No model, no I/O; every assertion carries the
evidence string the gate demands. It is deliberately conservative: when it
cannot tell, it says nothing and the ambiguity stays observed.
"""
from __future__ import annotations

from ..graph_api import Ambiguity, Assertion

ARBITER_ID = "provledger.heuristic"


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (stdlib only)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _name(qn: str) -> str:
    return qn.rsplit(".", 1)[-1]


class HeuristicArbiter:
    arbiter_id = ARBITER_ID

    def arbitrate(self, ambiguities: list[Ambiguity]) -> list[Assertion]:
        out: list[Assertion] = []
        for amb in ambiguities:
            chosen: dict[str, tuple[str, int]] = {}          # prev_key -> (cur_qn, d)
            for c in amb.cur:
                scored = sorted((edit_distance(_name(c.qualified_name), _name(p.qualified_name)), i)
                                for i, p in enumerate(amb.prev))
                if not scored:
                    continue
                d, i = scored[0]
                if len(scored) > 1 and scored[1][0] == d:
                    continue                                   # the minimum is not unique: abstain
                p = amb.prev[i]
                if p.struct_sig != c.struct_sig or p.dataflow_sig != c.dataflow_sig:
                    continue                                   # the signatures disagree: abstain
                if p.node_key in chosen:
                    chosen[p.node_key] = ("", -1)               # two currents claim one previous: abstain on both
                    continue
                chosen[p.node_key] = (c.qualified_name, d)
            for key, (cur_qn, d) in chosen.items():
                if cur_qn:
                    out.append(Assertion(cur_qualified_name=cur_qn, chosen_prev_key=key,
                                         evidence=f"name distance {d}, struct/dataflow sigs equal",
                                         arbiter=self.arbiter_id))
        return out
