"""022 — a node "has records" only if a person or a model said something.

On the dogfood repo 1679 of 2847 nodes carried a badge, almost all of them
close-time `derived` rows a rule wrote. A badge that is on 59% of the graph
tells a reader nothing, and `story` mode built on it selected 1601 nodes.

A derived-only node is not a node with a story: the badge now counts stated,
asserted, rejected alternatives and active constraints. `minor` and the derived
count stay visible, so nothing is hidden — it is just no longer called a story.
"""
import pytest


def _badge(conn, key="nk_a"):
    row = conn.execute("SELECT badge, reasons, derived, minor FROM node_badge_v WHERE node_key = ?", (key,)).fetchone()
    return dict(zip(("badge", "reasons", "derived", "minor"), row)) if row else None


def _reason(conn, tier, rule_id=None, role="reason", n=1):
    """Through provenance.insert_reason, which DERIVES the tier from the inputs —
    the schema enforces `stated` iff a verbatim span, so a raw insert cannot
    fake one, which is the point of the invariant."""
    from orchestrator import provenance as pv
    for i in range(n):
        kw = dict(project="demo", plan_id="P1", node_key="nk_a", kind="technical", role=role,
                  recorded_by="system", commit=False)
        if tier == "stated":
            u = pv.insert_utterance(conn, session_id="s", project="demo", plan_id="P1",
                                    text="keep paid orders only", occurred_at="2026-09-16 00:00:00")
            kw["verbatim"] = (u, 0, len("keep paid orders only"))
        elif tier == "derived":
            kw.update(rule_id=rule_id or "R5", interpretation=f"rule said so {i}")
        else:
            kw["interpretation"] = f"the agent's reading {i}"
        pv.insert_reason(conn, **kw)
    conn.commit()


def test_a_derived_only_node_carries_no_badge(conn):
    _reason(conn, "derived", rule_id="R5", n=16)
    b = _badge(conn)
    assert b["badge"] == 0, "16 rule-written rows must not read as a story"
    assert b["derived"] == 16, "the derived rows are still counted, just not as a badge"


def test_a_stated_or_asserted_reason_does_carry_one(conn):
    _reason(conn, "stated")
    assert _badge(conn)["badge"] == 1
    _reason(conn, "asserted")
    assert _badge(conn)["badge"] == 2


def test_a_constraint_or_a_rejected_alternative_counts(conn):
    from orchestrator import constraints as oc
    oc.record_constraint(conn, project="demo", subjects=["nk_a"], statement="keep paid orders only",
                         rationale="finance reconciles on paid orders", why_ref="docs/f.md", why_visibility="shared")
    conn.commit()
    assert _badge(conn)["badge"] == 1
    _reason(conn, "derived", role="rejected_path")
    assert _badge(conn)["badge"] == 2
