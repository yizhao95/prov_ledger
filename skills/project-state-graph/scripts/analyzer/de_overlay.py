"""DE overlay: assemble upstream -> operation -> downstream data lineage.

Reads the source-reference edges that sql_refs / api_refs already produced
(`reads_sql`, `writes_sql`, `reads_api`, `writes_api`) and, for every function
that both reads at least one source and writes at least one target, draws a
`lineage` edge from each upstream source to each downstream target, labelling
the function as the operation. This makes a data-engineering pipeline's
end-to-end flow (upstream table/api -> op -> downstream table) explicit and
queryable, multi-source aware.
"""
from __future__ import annotations

import sqlite3
from typing import Dict, List

from . import store

_READ_EDGES = ("reads_sql", "reads_api")
_WRITE_EDGES = ("writes_sql", "writes_api")


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    lineage_e = store.get_or_create_edge_type(conn, "lineage")

    reads = _by_function(conn, _READ_EDGES)    # fn_id -> [source_id, ...]
    writes = _by_function(conn, _WRITE_EDGES)  # fn_id -> [target_id, ...]
    fn_name = _function_names(conn)

    seen = set()
    for fn_id, sources in reads.items():
        targets = writes.get(fn_id)
        if not targets:
            continue
        op = fn_name.get(fn_id, str(fn_id))
        for src in sources:
            for dst in targets:
                if src == dst:
                    continue
                key = (src, dst, fn_id)
                if key in seen:
                    continue
                seen.add(key)
                store.add_edge(conn, lineage_e, src, dst,
                               metadata={"op": op})


def _by_function(conn, edge_types) -> Dict[int, List[int]]:
    out: Dict[int, List[int]] = {}
    for et in edge_types:
        rows = conn.execute(
            """SELECT e.src_node_id, e.dst_node_id
               FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
               WHERE t.name=?""",
            (et,),
        ).fetchall()
        for fn_id, source_id in rows:
            out.setdefault(int(fn_id), []).append(int(source_id))
    return out


def _function_names(conn) -> Dict[int, str]:
    rows = conn.execute(
        """SELECT n.id, n.name
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function', 'method')"""
    ).fetchall()
    return {int(i): n for i, n in rows}
