"""ask.scope — what was searched (DP phase 2e, Task 1; spec §21, J4).

An absence only means something next to its range, so every answer carries one
line saying how far the code looked: how many nodes, constraints, influencing
records and changes were in the table, the span of dates they cover, how many
candidates the matchers proposed and how many the model kept, and what was cut.
The numbers are counted off the fact table, never estimated.
"""
from __future__ import annotations


def scope(ft: dict, *, candidates: int = 0, chosen: int | None = None, truncated: dict | None = None) -> dict:
    nodes = ft.get("nodes") or []
    dates: list[str] = []
    for n in nodes:
        for key in ("first_seen", "last_changed"):
            if n.get(key):
                dates.append(n[key])
        for r in n.get("constraints", []) + n.get("reasons", []) + n.get("rejected_paths", []):
            if r.get("occurred_at"):
                dates.append(r["occurred_at"][:10])
        for r in n.get("changes", []):
            if r.get("at"):
                dates.append(r["at"][:10])
        for r in n.get("influence", []):
            if r.get("at"):
                dates.append(r["at"][:10])
    cut = dict(ft.get("truncated") or {})
    cut.update(truncated or {})
    return {"nodes": len(nodes),
            "constraints": sum(len(n.get("constraints", [])) for n in nodes),
            "influencing": sum(len(n.get("influence", [])) for n in nodes),
            "changes": sum(len(n.get("changes", [])) for n in nodes),
            "expectations": sum(len(n.get("expectations", [])) for n in nodes),
            "span": [min(dates), max(dates)] if dates else [None, None],
            "candidates": candidates,
            "chosen": len(nodes) if chosen is None else chosen,
            "truncated": cut}


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def line(sc: dict, lang: str = "en") -> str:
    """One English sentence (Chinese for lang='zh') — printed with every answer."""
    span = sc.get("span") or [None, None]
    if lang == "zh":
        rng = f"{span[0]} 至 {span[1]}" if span[0] else "没有带日期的记录"
        cut = ("裁剪：" + " · ".join(f"{k} {v}" for k, v in sorted(sc["truncated"].items()))) if sc.get("truncated") else "无裁剪"
        return (f"检索范围：节点 {sc['nodes']} · 约束 {sc['constraints']} · 有影响记录 {sc['influencing']} · "
                f"变更 {sc['changes']} · {rng}；候选 {sc['candidates']}，选中 {sc['chosen']}；{cut}。")
    rng = f"{span[0]} to {span[1]}" if span[0] else "no dated record"
    cut = (", ".join(f"{v} {k} truncated" for k, v in sorted(sc["truncated"].items()))) if sc.get("truncated") else "nothing truncated"
    return (f"Scope: {_n(sc['nodes'], 'node')}, {_n(sc['constraints'], 'constraint')}, "
            f"{_n(sc['influencing'], 'influencing record')}, {_n(sc['changes'], 'change')}, {rng}; "
            f"{_n(sc['candidates'], 'candidate')}, {sc['chosen']} chosen; {cut}.")
