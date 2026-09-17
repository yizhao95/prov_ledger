"""export — a whitelisted bundle, and the rows that are refused entry
(DP phase 3, Task 2; spec §8, E1 veto).

Handing the ledger to someone who was not there is the second core of the
product, and it is also the one place where a mistake cannot be taken back: a
file that left the machine has left the machine. So the boundary is not a
convention and not a reviewer's habit — it is **code, and it fails loudly**:

  - the bundle is a **whitelist**. A table, a column or a row that nobody
    explicitly listed does not travel. Adding a column to `change_reason` does
    not silently widen the export.
  - `utterance` is never exported. Not "usually not" — never. Verbatim words
    are the one thing the ledger holds that is always somebody's own.
  - a `personal` statement, reference or rationale is refused entry, and the
    manifest says how many were refused. A count is not a leak; a silence is.
  - `--include-rationale <id,…>` is per record and explicit. Asking for a
    rationale that is personal is an error (`ExportViolation`) naming the id —
    not a quiet skip, because the person asking deserves to know their request
    was not honoured.
  - after the files are written, `bundle()` reads them back and greps them for
    every personal string this project holds. If one is in there, the bundle is
    deleted and `ExportViolation` is raised. The whitelist is the design; the
    scan is what makes the design checkable.

The manifest carries the chain heads and the newest git anchor, so a bundle can
be checked against the repository it came from — see `integrity.CLAIM` for
exactly how much that proves.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone

from . import integrity, psg_bridge

FORMATS = ("md", "json", "zip")

# Containment on a very short string is not evidence of a leak — "ok" appears in
# half the English language. Strings below this length are counted in the
# manifest as unscanned instead of being silently ignored.
SCAN_MIN_LEN = 12

# Every column that may travel, per table. Anything not named here stays home.
WHITELIST: dict[str, tuple[str, ...]] = {
    "change_reason": ("id", "project", "node_key", "plan_id", "step_id", "kind", "role", "tier",
                      "evidence_level", "state", "occurred_at", "recorded_at", "recorded_by",
                      "rule_id", "significance", "superseded_by", "hash"),
    "reference": ("id", "project", "kind", "uri", "label", "occurred_at", "registered_at",
                  "verifiability", "hash"),
    "declared_node": ("id", "project", "slug", "qualified_name", "node_type", "description",
                      "attrs_json", "links_json", "links_checked", "state", "tier", "version",
                      "recorded_by", "occurred_at", "hash"),
    "expectation": ("id", "plan_id", "step_id", "project", "target", "target_kind", "claim",
                    "channel", "created_at"),
    "outcome": ("id", "expectation_id", "kind", "source", "tier", "reason", "observed_at"),
    "headline_finding": ("plan_id", "computed_at", "finding_id", "kind", "severity", "tier", "text"),
    "headline_response": ("plan_id", "finding_id", "action", "by", "at"),   # never `rationale`
    "node": ("node_key", "qualified_name", "events", "records"),
}
NEVER_EXPORTED = ("utterance",)


class ExportViolation(RuntimeError):
    """A personal row reached, or would have reached, the bundle. Nothing ships."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "node"


def _pick(row: sqlite3.Row | dict, table: str) -> dict:
    d = dict(row)
    return {k: d.get(k) for k in WHITELIST[table] if k in d}


# ── the personal rows, named so the scan can look for them ────────────────────

def personal_strings(conn, project: str) -> list[str]:
    """Every piece of text this project holds that must not appear in a bundle."""
    out: list[str] = []
    for (text,) in conn.execute("SELECT text FROM utterance WHERE (project = ? OR project IS NULL) AND visibility = 'personal'", (project,)):
        out.append(text or "")
    for (stmt, interp) in conn.execute("SELECT statement, interpretation FROM change_reason WHERE project = ? AND statement_visibility = 'personal'", (project,)):
        out += [stmt or "", interp or ""]
    for (rat,) in conn.execute("SELECT rationale FROM change_reason WHERE project = ? AND rationale_visibility = 'personal'", (project,)):
        out.append(rat or "")
    for (label, uri) in conn.execute("SELECT label, uri FROM reference WHERE project = ? AND visibility = 'personal'", (project,)):
        out += [label or "", uri or ""]
    return [s.strip() for s in out if s and s.strip()]


# ── gathering ─────────────────────────────────────────────────────────────────

def _reasons(conn, project: str, include_rationale: frozenset, skipped: dict) -> tuple[list[dict], set[int]]:
    rows = conn.execute(
        "SELECT r.*, u.visibility AS utterance_visibility, "
        "       substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start) AS quoted "
        "FROM change_reason_v r LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id "
        "WHERE r.project = ? ORDER BY r.id", (project,)).fetchall()
    out: list[dict] = []
    reason_ids: set[int] = set()
    for row in rows:
        d = dict(row)
        if d["statement_visibility"] != "shareable":
            skipped["personal_statement"] += 1
            continue
        rec = _pick(d, "change_reason")
        rec["chain"] = "change_reason"
        # the text: an interpretation or a statement travels; a quotation only
        # travels when the words it quotes were marked shareable
        if d.get("verbatim_utterance_id") is not None:
            if d.get("utterance_visibility") == "shareable":
                rec["text"] = d.get("quoted")
                rec["quoted_from_utterance"] = d["verbatim_utterance_id"]
            else:
                rec["text"] = None
                rec["quoted_withheld"] = (f"quoted from utterance #{d['verbatim_utterance_id']}, "
                                          "which is personal — the words stay home")
                skipped["personal_quote"] += 1
        else:
            rec["text"] = d.get("interpretation") or d.get("statement")
        if d["id"] in include_rationale:
            if not d.get("rationale"):
                raise ExportViolation(f"--include-rationale names reason #{d['id']}, which has no rationale")
            if d.get("rationale_visibility") != "shareable":
                raise ExportViolation(
                    f"--include-rationale names reason #{d['id']}, whose rationale is personal — refused (E1). "
                    "Mark it shareable in the ledger first; the export will not decide that for you.")
            rec["rationale"] = d["rationale"]
        elif d.get("rationale"):
            skipped["rationale_not_requested"] += 1
        out.append(rec)
        reason_ids.add(int(d["id"]))
    return out, reason_ids


def _references(conn, project: str, reason_ids: set[int], skipped: dict) -> tuple[list[dict], dict[int, list[int]]]:
    out: list[dict] = []
    links: dict[int, list[int]] = {}
    seen: set[int] = set()
    for row in conn.execute(
            "SELECT f.*, l.reason_id FROM reference_link l JOIN reference f ON f.id = l.reference_id "
            "WHERE f.project = ? ORDER BY f.id", (project,)).fetchall():
        d = dict(row)
        rid = int(d["reason_id"])
        if rid not in reason_ids:
            continue
        if d["visibility"] != "shareable":
            if d["id"] not in seen:
                skipped["personal_reference"] += 1
                seen.add(int(d["id"]))
            continue
        links.setdefault(rid, []).append(int(d["id"]))
        if d["id"] in seen:
            continue
        seen.add(int(d["id"]))
        rec = _pick(d, "reference")
        rec["chain"] = "reference"
        out.append(rec)
    return out, links


def _declared(conn, project: str) -> list[dict]:
    out = []
    for row in conn.execute(
            "SELECT * FROM declared_node WHERE project = ? AND superseded_by IS NULL AND state = 'active' "
            "ORDER BY id", (project,)).fetchall():
        rec = _pick(row, "declared_node")
        rec["chain"] = "declared_node"
        out.append(rec)
    return out


def _expectations(conn, project: str) -> tuple[list[dict], list[dict]]:
    exps = [dict(_pick(r, "expectation"), chain="expectation")
            for r in conn.execute("SELECT * FROM expectations WHERE project = ? ORDER BY id", (project,))]
    ids = {e["id"] for e in exps}
    outs = []
    for r in conn.execute("SELECT * FROM outcomes ORDER BY id"):
        if int(r["expectation_id"]) in ids:
            outs.append(dict(_pick(r, "outcome"), chain="outcome"))
    return exps, outs


def _finding_reason_ids(f: dict) -> set[int]:
    ev = f.get("evidence") or {}
    out: set[int] = set()
    if isinstance(ev.get("reason_id"), int):
        out.add(ev["reason_id"])
    for rid in (ev.get("reason_ids") or ()):
        if isinstance(rid, int):
            out.add(rid)
    return out


def _headlines(conn, project: str, reason_ids: set[int], skipped: dict) -> tuple[list[dict], list[dict]]:
    """A finding's text carries the words recorded at the time (DP phase 2d), so
    a finding built on a personal record is itself personal and stays home."""
    findings: list[dict] = []
    responses: list[dict] = []
    for h in conn.execute("SELECT * FROM headline WHERE project = ? ORDER BY id", (project,)).fetchall():
        try:
            parsed = json.loads(h["findings_json"] or "{}")
        except ValueError:
            parsed = {}
        items = parsed.get("findings", []) if isinstance(parsed, dict) else (parsed or [])
        for f in items:
            if not _finding_reason_ids(f) <= reason_ids:
                skipped["personal_finding"] += 1
                continue
            findings.append({"chain": "headline_finding", "plan_id": h["plan_id"], "computed_at": h["computed_at"],
                             "finding_id": f.get("id"), "kind": f.get("kind"),
                             "severity": f.get("severity"), "tier": f.get("tier"), "text": f.get("text")})
        for r in conn.execute("SELECT * FROM headline_response WHERE headline_id = ? ORDER BY id", (h["id"],)):
            # the action and who took it travel; the rationale is the person's
            # own wording and stays home unless it was recorded as a reason
            responses.append({"chain": "headline_response", "plan_id": h["plan_id"],
                              "finding_id": r["finding_id"], "action": r["action"], "by": r["by"], "at": r["at"]})
    return findings, responses


def _nodes(conn, project: str, reasons: list[dict], psg_db_path: str | None) -> list[dict]:
    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    counts: dict[str, int] = {}
    for r in reasons:
        if r.get("node_key"):
            counts[r["node_key"]] = counts.get(r["node_key"], 0) + 1
    out = []
    for key in sorted(counts):
        qn = (psg_bridge.latest_qualified_name(psg, key) if psg else None) or key
        events = psg_bridge._query(psg, "SELECT COUNT(*) FROM node_event WHERE node_key = ?", (key,)) if psg else []
        out.append({"chain": "node", "node_key": key, "qualified_name": qn,
                    "events": (int(events[0][0]) if events else None), "records": counts[key]})
    return out


# ── writing ───────────────────────────────────────────────────────────────────

def _node_md(node: dict, reasons: list[dict], refs_by_id: dict, links: dict) -> str:
    out = [f"# {node['qualified_name']}", "",
           f"- node key: `{node['node_key']}`",
           f"- events observed: {node['events'] if node['events'] is not None else 'graph unavailable'}",
           f"- records in this bundle: {node['records']}", ""]
    for label, role in (("Constraints", "constraint"), ("Reasons", "reason"), ("Rejected paths", "rejected_path")):
        items = [r for r in reasons if r.get("node_key") == node["node_key"] and r.get("role") == role]
        if not items:
            continue
        out += [f"## {label}", ""]
        for r in items:
            head = (f"- #{r['id']} · {r['tier']} · {(r.get('occurred_at') or '')[:10]} · {r.get('recorded_by')}"
                    f" · plan {r.get('plan_id')}")
            if r.get("state") and r["state"] != "active":
                head += f" · {r['state']}"
            out.append(head)
            if r.get("text"):
                out.append(f"  - {r['text']}")
            if r.get("quoted_withheld"):
                out.append(f"  - _{r['quoted_withheld']}_")
            if r.get("rationale"):
                out.append(f"  - why: {r['rationale']}")
            for ref_id in links.get(r["id"], ()):
                ref = refs_by_id[ref_id]
                out.append(f"  - source: {ref['kind']} · {ref['label']}" + (f" · {ref['uri']}" if ref.get("uri") else ""))
        out.append("")
    return "\n".join(out)


def _readme(project: str, manifest: dict) -> str:
    c, s = manifest["counts"], manifest["skipped"]
    anchor = manifest["anchor"]
    lines = [f"# {project} — decision provenance bundle", "",
             f"Exported {manifest['exported_at']} by provledger.", "",
             "## What is in here", "",
             "| file | what it holds |", "|---|---|",
             "| `manifest.json` | counts, what was left out, the chain heads, the git anchor |",
             "| `records.jsonl` | one line per exported record, whitelisted columns only |",
             "| `nodes/*.md` | one file per node: its constraints, reasons and rejected paths |", "",
             "## Counts", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(c.items())]
    lines += ["", "## What was left out, and why", "",
              "The export is a whitelist enforced in code, not a convention:", "",
              "- `utterance` rows are never exported. Verbatim words are always somebody's own.",
              f"- {s['personal_statement']} record(s) whose statement is marked personal were refused entry.",
              f"- {s['personal_quote']} record(s) quote words from a personal utterance; the record travels, the quotation does not.",
              f"- {s['personal_reference']} source(s) marked personal were refused entry.",
              f"- {s['rationale_not_requested']} rationale(s) exist but were not listed in `--include-rationale`, so they stayed home.",
              f"- {s['personal_finding']} headline finding(s) quote a record that is personal, so the finding stayed home too.",
              f"- rationales explicitly included: {manifest['included_rationale'] or 'none'}.", "",
              "## Integrity", ""]
    for table in integrity.CHAINS:
        h = manifest["chain_heads"][table]
        lines.append(f"- chain `{table}`: {h['rows']} row(s), head #{h['id']} `{(h['hash'] or '')[:12]}`")
    if anchor:
        lines.append(f"- git anchor: note `{(anchor.get('note_sha') or '')[:12]}` @ commit "
                     f"`{(anchor.get('commit') or '')[:12]}` ({anchor.get('at')}, plan {anchor.get('plan_id')})")
    else:
        lines.append(f"- git anchor: not anchored ({manifest['anchor_reason']})")
    lines += ["", integrity.CLAIM, ""]
    return "\n".join(lines)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan(root: str, secrets: list[str]) -> list[dict]:
    """Read the bundle back and look for every personal string. The whitelist is
    the design; this is what makes the design checkable."""
    hits: list[dict] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            path = os.path.join(dirpath, name)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for s in secrets:
                if len(s) >= SCAN_MIN_LEN and s in text:
                    hits.append({"file": os.path.relpath(path, root), "text": s[:80]})
    return hits


# ── the entry point ───────────────────────────────────────────────────────────

def bundle(conn, project: str, out_dir: str, *, include_rationale=(), fmt: str = "md",
           repo=None, psg_db_path: str | None = None, exported_by: str = "agent",
           now: str | None = None, log: bool = True) -> dict:
    """Write `<out_dir>/<project>/…` and return the manifest.

    fmt: "md" (the default) writes README + records.jsonl + nodes/*.md;
    "json" writes README + records.jsonl only; "zip" writes the md bundle and
    then zips it next to the directory.
    """
    if fmt not in FORMATS:
        raise ValueError(f"fmt must be one of {FORMATS}, got {fmt!r}")
    include = frozenset(int(i) for i in include_rationale)
    skipped = {"personal_statement": 0, "personal_quote": 0, "personal_reference": 0,
               "rationale_not_requested": 0, "personal_finding": 0, "utterance": 0}
    skipped["utterance"] = conn.execute(
        "SELECT COUNT(*) FROM utterance WHERE project = ? OR project IS NULL", (project,)).fetchone()[0]

    reasons, reason_ids = _reasons(conn, project, include, skipped)
    missing = sorted(include - reason_ids)
    if missing:
        raise ExportViolation(
            f"--include-rationale names {missing}, which are not exportable records of {project!r} "
            "(unknown id, other project, or a personal statement that is not in the bundle at all)")
    refs, links = _references(conn, project, reason_ids, skipped)
    declared = _declared(conn, project)
    exps, outs = _expectations(conn, project)
    findings, responses = _headlines(conn, project, reason_ids, skipped)
    nodes = _nodes(conn, project, reasons, psg_db_path)

    root = os.path.join(out_dir, _safe_name(project))
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(os.path.join(root, "nodes"), exist_ok=True)

    records = reasons + refs + declared + exps + outs + findings + responses + nodes
    with open(os.path.join(root, "records.jsonl"), "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str) + "\n")

    refs_by_id = {r["id"]: r for r in refs}
    node_files = []
    if fmt != "json":
        for node in nodes:
            path = os.path.join(root, "nodes", _safe_name(node["qualified_name"]) + ".md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_node_md(node, reasons, refs_by_id, links))
            node_files.append(os.path.relpath(path, root))

    heads = integrity.chain_heads(conn)
    anchor, anchor_reason = None, "no repository was given to this export"
    if repo is not None:
        try:
            found = integrity.read_anchors(repo)
            good = [a for a in found if "unreadable" not in a]
            if good:
                latest = good[-1]
                anchor = {"note_sha": latest.get("note_sha"), "commit": latest.get("commit"),
                          "at": latest.get("at"), "plan_id": latest.get("plan_id")}
                anchor_reason = None
            else:
                anchor_reason = f"no note on refs/notes/{integrity.NOTES_REF} in {repo}"
        except integrity.AnchorError as e:
            anchor_reason = f"git unavailable: {e}"

    secrets = personal_strings(conn, project)
    manifest = {
        "project": project, "exported_at": now or _now(), "exported_by": exported_by, "format": fmt,
        "counts": {"change_reason": len(reasons), "reference": len(refs), "declared_node": len(declared),
                   "expectation": len(exps), "outcome": len(outs), "headline_finding": len(findings),
                   "headline_response": len(responses), "node": len(nodes), "records": len(records)},
        "skipped": skipped,
        "included_rationale": sorted(include),
        "never_exported": list(NEVER_EXPORTED),
        "whitelist": {k: list(v) for k, v in WHITELIST.items()},
        "chain_heads": heads,
        "anchor": anchor, "anchor_reason": anchor_reason,
        "personal_strings_checked": sum(1 for s in secrets if len(s) >= SCAN_MIN_LEN),
        "personal_strings_too_short_to_scan": sum(1 for s in secrets if len(s) < SCAN_MIN_LEN),
        "scan_min_length": SCAN_MIN_LEN,
        "claim": integrity.CLAIM,
    }
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write(_readme(project, manifest))

    hits = _scan(root, secrets)
    if hits:
        shutil.rmtree(root, ignore_errors=True)
        raise ExportViolation(
            "personal text reached the bundle — nothing was written: "
            + "; ".join(f"{h['file']}: {h['text']!r}" for h in hits[:5]))

    files = []
    for dirpath, _d, names in os.walk(root):
        for name in sorted(names):
            p = os.path.join(dirpath, name)
            files.append({"path": os.path.relpath(p, root), "sha256": _sha256(p),
                          "bytes": os.path.getsize(p)})
    manifest["files"] = sorted(files, key=lambda f: f["path"])
    manifest["dir"] = root

    if fmt == "zip":
        zip_path = root + ".zip"
        if os.path.exists(zip_path):
            os.remove(zip_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for entry in manifest["files"] + [{"path": "manifest.json"}]:
                src = os.path.join(root, entry["path"])
                if os.path.exists(src):
                    z.write(src, os.path.join(_safe_name(project), entry["path"]))
        manifest["zip"] = zip_path

    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True, ensure_ascii=False, default=str)
    if fmt == "zip":
        with zipfile.ZipFile(manifest["zip"], "a", zipfile.ZIP_DEFLATED) as z:
            z.write(os.path.join(root, "manifest.json"), os.path.join(_safe_name(project), "manifest.json"))

    if log:
        record_export(conn, manifest)
    return manifest


def record_export(conn, manifest: dict) -> int:
    """One append-only `export_log` row per bundle: what left, what was refused,
    and which rationales somebody asked for by id."""
    cur = conn.execute(
        "INSERT INTO export_log (project, out_dir, fmt, included_rationale_json, counts_json, skipped_json, "
        "chain_heads_json, anchor_note_sha, anchor_commit, manifest_sha256, exported_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (manifest["project"], manifest["dir"], manifest["format"],
         json.dumps(manifest["included_rationale"]), json.dumps(manifest["counts"], sort_keys=True),
         json.dumps(manifest["skipped"], sort_keys=True), json.dumps(manifest["chain_heads"], sort_keys=True),
         (manifest["anchor"] or {}).get("note_sha"), (manifest["anchor"] or {}).get("commit"),
         hashlib.sha256(json.dumps(manifest, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
         manifest["exported_by"]))
    conn.commit()
    return int(cur.lastrowid)
