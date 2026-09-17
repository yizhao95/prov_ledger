"""`provledger.declared` — the built-in provider for the world outside the code
(DP phase 2c, spec §18).

The analyzer's declared stage writes one graph node per ACTIVE declaration,
carrying the declaration itself in `metadata_json`. This provider projects
those rows into observations, and from then on the host does what it does for
every other node: matches them across runs, mints their keys, writes their
events. Nothing here decides a tier — `declared_tier` is a recorded fact about
who supplied the declaration (stated or asserted), and it is an ordinary
attribute, not a claim about the host's tier.

Identity, in three layers:

  qualname  `declared:<slug>` — the name the user's sentence was given. It is
            the identity: revising a declaration is the same node, declaring a
            second one is a new node (§18: we never judge whether two
            descriptions mean the same object).
  struct    the declaration's attributes, canonicalised. Edit them and the next
            analysis run computes a node_changed, exactly like a function body.
  dataflow  the links, each with whether its target is still IN the graph. So
            deleting the function a rule constrains moves the rule's dataflow
            signature — the rule did not change, its footing did, and the
            difference is recorded rather than guessed.

The provider reads only `ctx` (the graph it was handed). It never opens the
orchestrator database, which is what lets it stay pure under the conformance
suite while the thing it describes lives in another file.
"""
from __future__ import annotations

import hashlib
import json

from ..graph_api import MUTATIONS, NodeObservation, Signature

DECLARED_TYPES = ("external_system", "business_rule", "stakeholder_decision", "external_dataset", "manual_figure")
LINK_KINDS = ("declared_feeds", "declared_constrains", "declared_depends_on")
QN_PREFIX = "declared:"
TYPE_ID = "provledger.declared"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def struct_signature(attrs) -> str:
    """The declaration's attributes, canonicalised: what the declaration SAYS."""
    return _sha(json.dumps(dict(attrs or {}), sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def dataflow_signature(links, resolved) -> tuple[str | None, bool]:
    """The links and their footing. No links → no signature and `trivial`, so the
    host never matches two link-less declarations on an empty dataflow layer."""
    if not links:
        return None, True
    parts = sorted(f"{d['kind']}:{d['to']}:{1 if d['to'] in resolved else 0}" for d in links)
    return _sha("\n".join(parts)), False


def _links_of(meta: dict) -> list[dict]:
    out = []
    for d in meta.get("links") or ():
        if isinstance(d, dict) and isinstance(d.get("to"), str) and d["to"]:
            out.append({"to": d["to"], "kind": d.get("kind") if d.get("kind") in LINK_KINDS else LINK_KINDS[-1],
                        "by": d.get("by") or "user"})
    return out


class DeclaredProvider:
    type_id = TYPE_ID
    schema_version = 1
    requires: tuple[str, ...] = ()

    def extract(self, ctx) -> list[NodeObservation]:
        rows = list(ctx.node_rows(DECLARED_TYPES))
        if not rows:
            return []
        ids = [r[0] for r in rows]
        ph = ",".join("?" * len(ids))
        meta: dict[int, dict] = {}
        for nid, raw in ctx.conn_ro.execute(f"SELECT id, metadata_json FROM node WHERE id IN ({ph})", ids):
            try:
                doc = json.loads(raw or "{}")
            except ValueError:
                doc = {}
            meta[nid] = doc if isinstance(doc, dict) else {}
        wanted = sorted({d["to"] for m in meta.values() for d in _links_of(m)})
        resolved: set[str] = set()
        if wanted:
            wph = ",".join("?" * len(wanted))
            resolved = {r[0] for r in ctx.conn_ro.execute(
                f"SELECT DISTINCT qualified_name FROM node WHERE qualified_name IN ({wph})", wanted)}
        out: list[NodeObservation] = []
        for nid, node_type, qualified_name, _name, _path, _ls, _le, _dtype in rows:
            m = meta.get(nid) or {}
            if m.get("state") != "active":
                continue                     # a draft was never in the graph; a retired one has left it
            links = _links_of(m)
            value, trivial = dataflow_signature(links, resolved)
            attrs = {
                "declared_tier": m.get("tier") if m.get("tier") in ("stated", "asserted") else "asserted",
                "declared_state": "active",
                "declared_version": int(m.get("version") or 1),
                "declared_id": int(m.get("declared_id") or 0),
                "declared_attrs": dict(m.get("attrs") or {}),
                "declared_links": sorted(f"{d['kind']}:{d['to']}" for d in links),
                "links_total": len(links),
                "links_resolved": sum(1 for d in links if d["to"] in resolved),
            }
            out.append(NodeObservation(
                type_id=self.type_id, node_type=node_type, qualified_name=qualified_name,
                file_path=None, line_start=None, line_end=None,
                signatures=(Signature("qualname", qualified_name),
                            Signature("struct", struct_signature(m.get("attrs"))),
                            Signature("dataflow", value, trivial=trivial)),
                attrs=attrs, node_id=nid))
        out.sort(key=lambda o: (o.node_type, o.qualified_name))
        return out

    def attributes_schema(self):
        return {"required": ["declared_tier"],
                "types": {"declared_tier": "str", "declared_state": "str", "declared_version": "int",
                          "declared_id": "int", "declared_attrs": "dict", "declared_links": "list",
                          "links_total": "int", "links_resolved": "int"}}

    def declared_stability(self) -> dict[str, str]:
        # A declared node's identity is its slug, which no source-code mutation
        # can touch. Renaming, moving or deleting the function it points at
        # moves its DATAFLOW signature — a change, recorded, on a node that is
        # still the same node. So: preserved everywhere, and the corpus tests
        # that claim rather than skipping past it (see test_declared_conformance).
        return {m: "preserved" for m in MUTATIONS}
