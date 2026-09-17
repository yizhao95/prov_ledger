"""ask.card — the evidence card (DP phase 2e, Task 3; spec §21, J5).

One markdown (or HTML) file a person can send to someone who was not there:
the question, the answer with its ids, the absences, the scope, a timeline of
the records behind it with each record's hash, and the result of walking the
three hash chains at export time. A tampered row is named — "chain broken at
#12" — not swallowed.

What the card claims is deliberately narrow: **these records existed at export
time and had not been altered.** It does not claim they are true, and it says
so on its face. Anchoring the chain heads outside the database (git notes) is
phase 3 work, so until then the card prints "not anchored" rather than
implying an external witness it does not have.
"""
from __future__ import annotations

import html as html_mod
from datetime import datetime, timezone

from .. import provenance

CHAINS = ("change_reason", "utterance", "reference")
GIT_NOTES = "not anchored (external anchoring lands in phase 3)"
DISCLAIMER = ("This card shows that these records existed at export time and had not been altered. "
              "It does not show that they are true.")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def integrity(conn) -> dict:
    """verify_chain over the three chained tables, plus a flat ok / broken list."""
    out: dict = {"ok": True, "broken": [], "git_notes": GIT_NOTES}
    for table in CHAINS:
        res = provenance.verify_chain(conn, table)
        out[table] = res
        if not res["ok"]:
            out["ok"] = False
            out["broken"].append({"table": table, "first_bad_id": res["first_bad_id"]})
    return out


def timeline(conn, doc: dict) -> list[dict]:
    """The records behind the answer, oldest first, each with its hash and chain."""
    ft = doc["facts"]
    rows: list[dict] = []
    seen: set[tuple[str, int]] = set()

    def add(table: str, rid: int, cite: str, kind: str, node: str | None, at: str | None, text: str | None) -> None:
        if (table, rid) in seen:
            return
        seen.add((table, rid))
        h = conn.execute(f"SELECT hash FROM {table} WHERE id = ?", (rid,)).fetchone()
        rows.append({"cite": cite, "chain": table, "id": rid, "kind": kind, "node": node or "",
                     "at": (at or ""), "text": (text or "").replace("\n", " ").replace("|", "/"),
                     "hash": (h[0] if h else "") or ""})

    for node in ft["nodes"]:
        for kind in ("constraints", "reasons", "rejected_paths"):
            for r in node[kind]:
                add("change_reason", r["id"], r["cite"], kind[:-1] if kind.endswith("s") else kind,
                    node["qn"], r.get("occurred_at"), r.get("text"))
                for ref in r.get("references") or ():
                    row = conn.execute("SELECT occurred_at, kind, label FROM reference WHERE id = ?", (ref["cite"][2:],)).fetchone()
                    if row:
                        add("reference", int(ref["cite"][2:]), ref["cite"], f"source ({row[1]})", node["qn"], row[0], row[2])
                utt = conn.execute("SELECT u.id, u.occurred_at, u.text FROM change_reason r JOIN utterance u "
                                   "ON u.id = r.verbatim_utterance_id WHERE r.id = ?", (r["id"],)).fetchone()
                if utt:
                    add("utterance", utt[0], f"#u{utt[0]}", "quoted words", node["qn"], utt[1], utt[2])
    rows.sort(key=lambda r: (r["at"] or "", r["chain"], r["id"]))
    return rows


def _body(conn, doc: dict, now: str | None = None) -> dict:
    integ = integrity(conn)
    broken = [f"chain broken at #{b['first_bad_id']} ({b['table']})" for b in integ["broken"]]
    return {"integrity": integ, "broken": broken, "timeline": timeline(conn, doc), "exported_at": now or _now()}


def card_md(conn, doc: dict, *, now: str | None = None) -> str:
    """The card as markdown — what `--export` and `GET /ledger/card` hand over."""
    b = _body(conn, doc, now)
    ask_id = doc.get("ask_id")
    out = [f"# Evidence card · ask {ask_id}", "",
           f"**Question** {doc['question']}", "", "## Answer", ""]
    if doc["degraded"] or not doc["answer"]:
        out += [f"_{doc.get('note') or 'summary unavailable: no model'}_", "",
                "The fact table below is the answer.", ""]
    else:
        out += [doc["answer"], ""]
        if doc.get("note"):
            out += [f"_{doc['note']}_", ""]
    if doc.get("absences"):
        out += ["## Absences", ""] + [f"- {a['text']}" for a in doc["absences"]] + [""]
    out += ["## Scope", "", doc.get("scope_line") or "(not computed)", ""]
    out += ["## Timeline", "", "| occurred | record | kind | node | chain | hash |", "|---|---|---|---|---|---|"]
    for r in b["timeline"]:
        out.append(f"| {r['at'][:19] or '-'} | {r['cite']} | {r['kind']} | `{r['node']}` | {r['chain']} | `{r['hash'][:12]}` |")
    if not b["timeline"]:
        out.append("| - | - | no record behind this answer | - | - | - |")
    out += ["", "## Integrity at export time", ""]
    for table in CHAINS:
        res = b["integrity"][table]
        state = "ok" if res["ok"] else f"**chain broken at #{res['first_bad_id']}**"
        out.append(f"- chain `{table}`: {state} · {res['rows']} row(s) walked")
    out += [f"- git notes: {b['integrity']['git_notes']}", ""]
    out += ["## Fact table", "", "```", doc["facts_text"], "```", ""]
    out += ["## Provenance of this card", "",
            f"- ask id {ask_id}",
            f"- fact table sha `{doc.get('facts_sha', '')}`",
            f"- model: {doc.get('model') or 'none'} · runner: {doc.get('runner') or 'none'}",
            f"- project: {doc['project']}",
            f"- exported at {b['exported_at']}", "",
            DISCLAIMER, ""]
    return "\n".join(out)


def card_html(conn, doc: dict, *, now: str | None = None) -> str:
    """The same card as a standalone page — no script, no fetch, nothing live."""
    b = _body(conn, doc, now)
    e = html_mod.escape
    ask_id = doc.get("ask_id")
    rows = "\n".join(
        f"<tr><td>{e(r['at'][:19] or '-')}</td><td>{e(r['cite'])}</td><td>{e(r['kind'])}</td>"
        f"<td><code>{e(r['node'])}</code></td><td>{e(r['chain'])}</td><td><code>{e(r['hash'][:12])}</code></td></tr>"
        for r in b["timeline"]) or "<tr><td colspan=\"6\">no record behind this answer</td></tr>"
    chains = "\n".join(
        f"<li>chain <code>{e(t)}</code>: " +
        ("ok" if b["integrity"][t]["ok"] else f"<strong>chain broken at #{e(str(b['integrity'][t]['first_bad_id']))}</strong>") +
        f" &middot; {b['integrity'][t]['rows']} row(s) walked</li>" for t in CHAINS)
    answer = (f"<p><em>{e(doc.get('note') or 'summary unavailable: no model')}</em></p>"
              if (doc["degraded"] or not doc["answer"]) else f"<p>{e(doc['answer'])}</p>")
    absences = "".join(f"<li>{e(a['text'])}</li>" for a in doc.get("absences") or ())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Evidence card · ask {e(str(ask_id))}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:.3rem .5rem;text-align:left}}
pre{{background:#f6f6f6;padding:1rem;overflow:auto}}</style></head>
<body>
<h1>Evidence card &middot; ask {e(str(ask_id))}</h1>
<p><strong>Question</strong> {e(doc['question'])}</p>
<h2>Answer</h2>
{answer}
{f'<h2>Absences</h2><ul>{absences}</ul>' if absences else ''}
<h2>Scope</h2>
<p>{e(doc.get('scope_line') or '(not computed)')}</p>
<h2>Timeline</h2>
<table><thead><tr><th>occurred</th><th>record</th><th>kind</th><th>node</th><th>chain</th><th>hash</th></tr></thead>
<tbody>
{rows}
</tbody></table>
<h2>Integrity at export time</h2>
<ul>
{chains}
<li>git notes: {e(b['integrity']['git_notes'])}</li>
</ul>
<h2>Fact table</h2>
<pre>{e(doc['facts_text'])}</pre>
<h2>Provenance of this card</h2>
<ul>
<li>ask id {e(str(ask_id))}</li>
<li>fact table sha <code>{e(doc.get('facts_sha', ''))}</code></li>
<li>model: {e(str(doc.get('model') or 'none'))} &middot; runner: {e(str(doc.get('runner') or 'none'))}</li>
<li>project: {e(doc['project'])}</li>
<li>exported at {e(b['exported_at'])}</li>
</ul>
<p>{e(DISCLAIMER)}</p>
</body></html>
"""
