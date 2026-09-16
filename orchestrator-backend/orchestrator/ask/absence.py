"""ask.absence — the "no" sentences, computed (DP phase 2e, Task 1; spec §21, J3).

The hardest thing for a language model to say honestly is "there is nothing".
So it is never asked. Code reads four absences straight off the fact table:

  not_in_graph            the node is not in the project graph at all
  never_verified          no outcome was ever recorded for it
  no_alternatives_tested  one value, no rejected path, no second measurement
  unchanged_since         the day of its last change event

Each sentence ends in `[scope]`, not a record id — an absence is a statement
about the searched range, not about the world (spec §21, honest boundary).
"""
from __future__ import annotations

SCOPE_CITE = "[scope]"


def _sentence(text: str) -> str:
    return f"{text.rstrip().rstrip('.')}. {SCOPE_CITE}"


def absences(conn, ft: dict) -> list[dict]:
    """`[{code, node, text, cite, value}]` — one line per absence found."""
    out: list[dict] = []
    for n in ft.get("nodes") or []:
        qn = n["qn"]
        if n.get("status") != "existing":
            out.append({"code": "not_in_graph", "node": qn, "cite": SCOPE_CITE, "value": None,
                        "text": _sentence(f"`{qn}` is not in the project graph, and nothing in the ledger is anchored to it")})
            continue

        expectations = n.get("expectations") or []
        outcomes = [e for e in expectations if e.get("outcome")]
        if not outcomes:
            out.append({"code": "never_verified", "node": qn, "cite": SCOPE_CITE, "value": None,
                        "text": _sentence(f"`{qn}` has never been verified: no outcome is recorded for it in scope")})

        values = n.get("values") or []
        distinct_values = sorted({v["value"] for v in values})
        distinct_claims = {e["claim"] for e in expectations}
        comparable = bool(values or expectations)
        if comparable and not n.get("rejected_paths") and len(distinct_values) < 2 and len(distinct_claims) < 2:
            value = distinct_values[0] if distinct_values else (sorted(distinct_claims)[0] if distinct_claims else None)
            out.append({"code": "no_alternatives_tested", "node": qn, "cite": SCOPE_CITE, "value": value,
                        "text": _sentence(f"No alternative to {value} was tested for `{qn}`: no rejected path and no "
                                          f"second measured value in scope")})

        if n.get("last_changed"):
            out.append({"code": "unchanged_since", "node": qn, "cite": SCOPE_CITE, "value": n["last_changed"],
                        "text": _sentence(f"`{qn}` has not changed since {n['last_changed']}")})
    return out
