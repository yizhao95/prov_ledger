"""review_diff.py — git-diff vs deep-graph stale-reference review.

Part of the update-project-state-graph skill. Given a project's git repo and its
project-state-graph deep sqlite graph, this module:

  1. resolve_range(repo, registered_sha) -> (base, head, mode)  # registered|remote|local
     Base = the registered (graph-build) sha when it is an ancestor of HEAD;
     else merge-base with the upstream; else the raw sha. Never '@{u}..HEAD'
     (empty right after a push — FL-015).

  2. changed_symbols(repo, base, head) -> [{file, kind, old_name, new_name}]
     Parses `git diff` for removed/renamed top-level def/class symbols.

  3. stale_references(db_path, removed_symbols, changed_files=None) -> [{caller, callee, file, severity}]
     Queries the deep graph for edges whose destination node is a removed/renamed
     symbol — i.e. callers that still point at something that no longer exists.

  4. report(db_path, changed) -> {ok, gaps, text}
     Convenience: runs stale_references for the removed/renamed symbols and
     assembles a human-readable verdict. ok=False means gaps were found.

No third-party deps — stdlib only (subprocess, sqlite3, re).
"""
from __future__ import annotations

import re
import sqlite3
import subprocess


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    ).stdout


def _git_ok(repo: str, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=repo,
                          capture_output=True, text=True).returncode == 0


def resolve_range(repo: str, registered_sha: str) -> tuple[str, str, str]:
    """Return (base, head, mode). head is always 'HEAD'.

    registered: base = registered_sha — the commit the graph was built from —
                whenever it is a real ancestor of HEAD. This is the semantically
                right range ("what changed since the graph was last true").
    remote:     registered sha unknown/unrelated but an upstream exists →
                base = merge-base(@{u}, HEAD) (unpushed work), else the merge-base
                with the remote default branch.
    local:      neither → base = registered_sha as given (visible degradation).

    FL-015: never '@{u}..HEAD'. Right after `git push` the upstream IS HEAD, so
    that range is empty and a review over it inspects nothing while reporting
    changed_symbols=[] — "didn't look" masquerading as "nothing changed".
    """
    head = _git(repo, "rev-parse", "HEAD").strip()
    if registered_sha and _git_ok(repo, "cat-file", "-e", f"{registered_sha}^{{commit}}") \
            and _git_ok(repo, "merge-base", "--is-ancestor", registered_sha, "HEAD"):
        return (registered_sha, "HEAD", "registered")
    if _git_ok(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
        candidates = ["@{u}", "origin/HEAD", "origin/main", "origin/master"]
        for ref in candidates:
            if not _git_ok(repo, "rev-parse", "--verify", "-q", ref):
                continue
            mb = _git(repo, "merge-base", ref, "HEAD").strip()
            if mb and mb != head:
                return (mb, "HEAD", "remote")
    return (registered_sha, "HEAD", "local")


# Matches a removed (`-def foo(`) or added (`+def foo(`) TOP-LEVEL function/class
# header. PSG-C7: no `\s*` after the sign, so an indented method (`-    def m`) is
# NOT captured as if it were top-level (the rename heuristic relied on that).
_DEF_RE = re.compile(r"^([+-])(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")


def changed_symbols(repo: str, base: str, head: str) -> list[dict]:
    """Parse git diff for removed/renamed top-level def/class symbols.

    A symbol present on a removed line but absent from added lines is 'removed'.
    A symbol removed AND a different symbol added in the same file is reported as
    a 'renamed' pair (best-effort: 1 removed + 1 added in a file).

    PSG-C7: tracks the source (`--- a/`) and target (`+++ b/`) paths separately and
    handles `/dev/null`, so a fully deleted file's removed defs attach to that file
    instead of leaking onto the previous one.
    """
    diff = _git(repo, "diff", f"{base}..{head}")
    per_file_removed: dict[str, list[str]] = {}
    per_file_added: dict[str, list[str]] = {}
    a_file = None  # source path (from --- a/...), None for an added file
    b_file = None  # target path (from +++ b/...), None for a deleted file
    for line in diff.splitlines():
        if line.startswith("--- "):
            a_file = None if line.rstrip().endswith("/dev/null") else line[6:]
            continue
        if line.startswith("+++ "):
            b_file = None if line.rstrip().endswith("/dev/null") else line[6:]
            f = b_file or a_file  # attribute to the surviving path; a/ for deletes
            if f is not None:
                per_file_removed.setdefault(f, [])
                per_file_added.setdefault(f, [])
            continue
        m = _DEF_RE.match(line)
        if not m:
            continue
        f = b_file or a_file
        if f is None:
            continue
        sign, name = m.group(1), m.group(2)
        if sign == "-":
            per_file_removed.setdefault(f, []).append(name)
        else:
            per_file_added.setdefault(f, []).append(name)

    results: list[dict] = []
    for f, removed in per_file_removed.items():
        added = per_file_added.get(f, [])
        for name in removed:
            if name in added:
                continue  # unchanged (touched body but signature kept)
            # Heuristic rename: exactly one removed + one added that's new.
            new_only = [a for a in added if a not in removed]
            if len(removed) == 1 and len(new_only) == 1:
                results.append({"file": f, "kind": "renamed",
                                "old_name": name, "new_name": new_only[0]})
            else:
                results.append({"file": f, "kind": "removed",
                                "old_name": name, "new_name": None})
    return results


def _module_from_path(path: str | None) -> str | None:
    if not path:
        return None
    p = path[:-3] if path.endswith(".py") else path
    return p.replace("\\", "/").replace("/", ".")


def stale_references(db_path: str, removed_symbols: list[str],
                     changed_files: set[str] | None = None) -> list[dict]:
    """Find call edges in the deep graph whose destination is a removed/renamed
    symbol. `removed_symbols` are QUALIFIED names (`module.name`, as report()
    derives them from changed_symbols' file); a bare name still works but can
    only produce warnings.

    Returns [{caller, callee, file, severity, match, confidence}]:
      match "qualified_name" — dst.qualified_name == symbol (FL-029): the edge's
        confidence decides: high/NULL -> "fail", inferred -> "warning";
      match "bare_name"       — no qualified hit at all, dst.name == last segment:
        a guess, always "warning".
    FL-023: a caller whose file is in `changed_files` is a warning either way.
    """
    if not removed_symbols:
        return []
    changed_files = set(changed_files or ())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    has_conf = "confidence" in {r[1] for r in conn.execute("PRAGMA table_info(edge)")}
    conf_col = "e.confidence" if has_conf else "NULL"
    edge_type_filter = ""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='edge_type'").fetchone():
        edge_type_filter = "AND (et.name IS NULL OR et.name = 'calls')"
    sql = f"""
        SELECT src.name AS caller, src.qualified_name AS caller_q,
               dst.name AS callee, dst.qualified_name AS callee_q,
               src.file_path AS file, {conf_col} AS confidence
        FROM edge e
        JOIN node src ON src.id = e.src_node_id
        JOIN node dst ON dst.id = e.dst_node_id
        LEFT JOIN edge_type et ON et.id = e.edge_type_id
        WHERE {{where}} {edge_type_filter}
    """
    out: list[dict] = []
    for sym in removed_symbols:
        rows = conn.execute(sql.format(where="dst.qualified_name = ?"), (sym,)).fetchall()
        match = "qualified_name"
        if not rows:
            bare = sym.rsplit(".", 1)[-1]
            rows = conn.execute(sql.format(where="dst.name = ?"), (bare,)).fetchall()
            match = "bare_name"
        for r in rows:
            conf = r["confidence"] or "high"
            if match == "bare_name" or conf != "high" or r["file"] in changed_files:
                severity = "warning"
            else:
                severity = "fail"
            out.append({"caller": r["caller"], "callee": r["callee"], "file": r["file"],
                        "severity": severity, "match": match, "confidence": conf, "symbol": sym})
    conn.close()
    return out


def report(db_path: str, changed: list[dict], changed_files: set[str] | None = None) -> dict:
    """Assemble a stale-reference verdict from a list of changed-symbol dicts.

    changed: items shaped like changed_symbols() output (need 'old_name'/'kind').
    changed_files: the diff's file paths (FL-023) — hits whose caller lives in one
    of them are warnings. Returns {ok: bool, gaps: [...], text: str}; gaps carries
    every hit (each with its severity), ok=False iff a "fail" hit exists.
    """
    removed = []
    for c in changed:
        if c.get("kind") in ("removed", "renamed") and c.get("old_name"):
            mod = _module_from_path(c.get("file"))
            removed.append(f"{mod}.{c['old_name']}" if mod else c["old_name"])   # FL-029: qualified
    hits = stale_references(db_path, removed, changed_files=changed_files)
    if not hits:
        return {
            "ok": True,
            "gaps": [],
            "text": "No stale references found — the deep graph has no callers "
                    "pointing at removed/renamed symbols.",
        }
    fails = [h for h in hits if h["severity"] == "fail"]
    lines = ["Stale references found (callers still target removed/renamed symbols):"]
    for h in hits:
        if h.get("match") == "bare_name":
            note = " [warning: bare-name match only — the qualified symbol is not in the graph]"
        elif h.get("confidence", "high") != "high":
            note = f" [warning: {h['confidence']} call edge — a guess, not evidence]"
        elif h["severity"] == "warning":
            note = " [warning: caller's file is in this diff — confirm the call was dropped]"
        else:
            note = ""
        lines.append(f"  - {h['caller']} ({h['file']}) still calls {h['callee']}{note}")
    if not fails:
        lines.append("  all hits are warnings (bare-name / inferred / edited in the same diff) — not blocking")
    return {"ok": not fails, "gaps": hits, "text": "\n".join(lines)}


def data_drift(
    db_path: str,
    *,
    removed_columns: list[str] | None = None,
    dtype_changes: list[dict] | None = None,
    removed_datasets: list[str] | None = None,
) -> dict:
    """Data-level drift review (report, don't auto-fix).

    Flags, against the deep graph:
      - removed_columns : a column still present in the graph (still wired via
        has_column / transforms / produces-consumes) that the diff removed.
      - dtype_changes   : [{column, new_dtype}] where the new dtype disagrees
        with the dtype recorded in the graph (an e2e data break).
      - removed_datasets: a dataset node still referenced that the diff removed.

    Returns {ok, gaps, text}. ok=False iff any gap was found. NOTHING is mutated
    in the graph — a human decides, same philosophy as stale_references.
    """
    import json

    removed_columns = removed_columns or []
    dtype_changes = dtype_changes or []
    removed_datasets = removed_datasets or []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    gaps: list[dict] = []

    def _nodes_named(type_name: str, names: list[str]) -> list:
        if not names:
            return []
        ph = ",".join("?" for _ in names)
        return conn.execute(
            f"""SELECT n.id, n.name, n.metadata_json
                FROM node n JOIN node_type t ON n.node_type_id=t.id
                WHERE t.name=? AND n.name IN ({ph})""",
            (type_name, *names),
        ).fetchall()

    # removed columns still in the graph
    for row in _nodes_named("column", removed_columns):
        gaps.append({"kind": "removed_column",
                     "detail": f"column '{row['name']}' removed in diff but "
                               f"still present in graph (id={row['id']})"})

    # removed datasets still in the graph
    for row in _nodes_named("dataset", removed_datasets):
        gaps.append({"kind": "removed_dataset",
                     "detail": f"dataset '{row['name']}' removed in diff but "
                               f"still present in graph (id={row['id']})"})

    # dtype changes that disagree with the graph
    change_map = {c["column"]: c["new_dtype"] for c in dtype_changes if c.get("column")}
    for row in _nodes_named("column", list(change_map)):
        info = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        graph_dtype = info.get("dtype", info.get("type", "unknown"))
        new_dtype = change_map[row["name"]]
        if graph_dtype not in (None, "unknown") and new_dtype != graph_dtype:
            gaps.append({"kind": "dtype_change",
                         "detail": f"column '{row['name']}' dtype changed "
                                   f"{graph_dtype} -> {new_dtype}; downstream "
                                   f"consumers may break"})

    conn.close()

    if not gaps:
        return {"ok": True, "gaps": [],
                "text": "No data drift found — columns, dtypes and datasets "
                        "are consistent with the deep graph."}
    lines = ["Data drift found (review, do NOT auto-fix):"]
    for g in gaps:
        lines.append(f"  - [{g['kind']}] {g['detail']}")
    return {"ok": False, "gaps": gaps, "text": "\n".join(lines)}


# ── A-5 · combined verdict (ANDs all active contract gates) ──────────────────────

import contract_diff  # noqa: E402  (kept at bottom to avoid reordering legacy API)


def full_verdict(
    db_path: str,
    repo: str,
    base: str,
    head: str,
    *,
    changed: list[dict] | None = None,
    removed_columns: list[str] | None = None,
    dtype_changes: list[dict] | None = None,
    removed_datasets: list[str] | None = None,
    dataframe_deltas: list[dict] | None = None,
    registered_sha: str | None = None,
) -> dict:
    """The Phase A combined review verdict — the AND of all active gates.

    Gates:
      - range_nonempty    (E6-4: HEAD moved since the graph was built but the
                           range has no changed files -> we did not look; FAIL.
                           Only active when `registered_sha` is given — read
                           and locked BEFORE any refresh, see SKILL.md step 0)
      - stale_references  (code-symbol: removed/renamed callees still referenced)
      - data_drift        (basic data: removed col/dataset, dtype disagreement)
      - signature         (A-1: changed Python signatures vs callers/consumers)
      - dataframe_schema  (A-2: per-fn column-set/dtype deltas — caller-supplied)
      - sql_contract      (A-3: changed .sql projection vs downstream readers)

    `changed` is the changed_symbols() output (for stale_references). Data-level
    deltas are supplied by the caller, derived from the data-model analyzer on
    base vs head. Each `dataframe_deltas` item: {fn_qname, base_cols, head_cols}.

    Returns {ok, gaps, text, gates:{name: bool}}. ok is False iff any gate is
    False.
    """
    changed = changed or []
    files = set(contract_diff.changed_files(repo, base, head))
    # E6-4: "didn't look" must never read as "nothing changed".
    head_sha = _git(repo, "rev-parse", "HEAD").strip()
    range_nonempty = not (registered_sha and head_sha != registered_sha and not files)

    stale = report(db_path, changed, changed_files=files)   # FL-023
    drift = data_drift(db_path, removed_columns=removed_columns,
                       dtype_changes=dtype_changes,
                       removed_datasets=removed_datasets)
    sig = contract_diff.signature_contract(db_path, repo, base, head)
    sql = contract_diff.sql_contract(db_path, repo, base, head)

    df_reports = []
    for delta in (dataframe_deltas or []):
        df_reports.append(contract_diff.dataframe_schema_contract(
            db_path, delta["fn_qname"],
            base_cols=delta.get("base_cols", {}),
            head_cols=delta.get("head_cols", {}),
            changed_files=files))
    df_ok = all(r["ok"] for r in df_reports)
    df_gaps = [g for r in df_reports for g in r["gaps"]]

    gates = {
        "range_nonempty": range_nonempty,
        "stale_references": stale["ok"],
        "data_drift": drift["ok"],
        "signature": sig["ok"],
        "dataframe_schema": df_ok,
        "sql_contract": sql["ok"],
    }
    ok = all(gates.values())

    gaps = {
        "range_nonempty": ([] if range_nonempty else
                           [f"empty range {base}..{head} while HEAD {head_sha[:7]} != registered {registered_sha[:7]}"]),
        "stale_references": stale["gaps"],
        "data_drift": drift["gaps"],
        "signature": sig["gaps"],
        "dataframe_schema": df_gaps,
        "sql_contract": sql["gaps"],
    }

    lines = [f"Combined review verdict: {'PASS' if ok else 'FAIL'}"]
    for name, passed in gates.items():
        lines.append(f"  [{'ok' if passed else 'FAIL'}] {name}")
    if not range_nonempty:
        lines.append(f"  [FAIL] range_nonempty: empty range {base}..{head} while HEAD {head_sha[:7]} "
                     f"!= registered {registered_sha[:7]} — the review looked at nothing; "
                     "lock registered_sha before refreshing (SKILL.md step 0)")
    for name, rep_obj in (("stale_references", stale), ("data_drift", drift),
                          ("signature", sig), ("sql_contract", sql)):
        if not rep_obj["ok"]:
            lines.append(rep_obj["text"])
    for r in df_reports:
        if not r["ok"]:
            lines.append(r["text"])

    return {"ok": ok, "gaps": gaps, "text": "\n".join(lines), "gates": gates}
