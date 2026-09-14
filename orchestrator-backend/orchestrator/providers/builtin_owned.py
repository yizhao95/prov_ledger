"""Reference NodeTypeProvider for owned data nodes — dataframe / column —
whose identity is their owner's identity plus a local name (spec §2.2, phase 6
Task 3). Signatures: qualname only; the host's owner layer re-links them when
their owner is paired, which is why conformance runs this provider with the
symbol provider as companion.
"""
from __future__ import annotations

from ..graph_api import MUTATIONS, NodeObservation, Signature

OWNED_TYPES = ("dataframe", "column")


def _owner_fn(fn_spans: list[tuple[int, int | None, str]], line: int | None) -> str | None:
    """Innermost function/method (by start line) whose span contains `line`."""
    if line is None:
        return None
    best = None
    for ls, le, qn in fn_spans:
        if ls <= line and (le is None or line <= le) and (best is None or ls > best[0]):
            best = (ls, qn)
    return best[1] if best else None


class BuiltinOwnedProvider:
    type_id = "provledger.owned"
    schema_version = 1
    requires: tuple[str, ...] = ()

    def extract(self, ctx) -> list[NodeObservation]:
        fns = ctx.node_rows(("function", "method"))
        fn_spans: dict[str, list[tuple[int, int | None, str]]] = {}
        for nid, _t, qn, name, path, ls, le, _d in fns:
            if path and ls:
                fn_spans.setdefault(path, []).append((ls, le, qn or name))
        owner_of_col = {dst: src for src, dst in ctx.conn_ro.execute(
            """SELECT e.src_node_id, e.dst_node_id FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
               WHERE t.name='has_column'""")}
        out: list[NodeObservation] = []
        by_type: dict[str, list] = {}
        for r in ctx.node_rows(OWNED_TYPES):
            by_type.setdefault(r[1], []).append(tuple(r))
        for nid, _t, _qn, name, path, ls, le, _d in sorted(by_type.get("dataframe", []), key=lambda r: (r[4] or "", r[5] or 0, r[0])):
            owner = _owner_fn(fn_spans.get(path, []), ls) or (path or "?")
            out.append(NodeObservation(
                type_id=self.type_id, node_type="dataframe", qualified_name=f"{owner}:{name}", file_path=path,
                line_start=ls, line_end=le, signatures=(Signature("qualname", f"{owner}:{name}"),),
                attrs={}, owner_qn=owner, name=name, node_id=nid))
        for nid, _t, _qn, name, path, ls, le, dtype in sorted(by_type.get("column", []), key=lambda r: (r[0],)):
            oid = owner_of_col.get(nid)
            owner = None
            if oid is not None:
                row = ctx.conn_ro.execute("SELECT qualified_name, name FROM node WHERE id=?", (oid,)).fetchone()
                owner = (row[0] or row[1]) if row else None
            owner = owner or (path or "?")
            attrs = {"dtype": dtype or "unknown"}
            if oid is not None:
                attrs["owner_node_id"] = oid          # the host re-links to the owner's snapshot name
            out.append(NodeObservation(
                type_id=self.type_id, node_type="column", qualified_name=f"{owner}.{name}", file_path=path,
                line_start=ls, line_end=le, signatures=(Signature("qualname", f"{owner}.{name}"),),
                attrs=attrs, owner_qn=owner, name=name, node_id=nid))
        return out

    def attributes_schema(self):
        return {"types": {"dtype": "str", "owner_node_id": "int"}}

    def declared_stability(self) -> dict[str, str]:
        # Identity = owner identity + LOCAL NAME: renaming the variable that
        # holds a dataframe, or moving it into an extracted function, is a new
        # data node by design (spec §2.2) — declared, not hidden.
        d = {m: "preserved" for m in MUTATIONS}
        d["rename_variable"] = "broken"
        d["extract_function"] = "broken"
        d["delete_function"] = "broken"           # a deleted owner takes its dataframes/columns with it
        d["swap_two_similar"] = "broken"          # an ambiguous owner cannot pass identity down: the owned node is dropped
        return d
