"""plan_read — `provledger plan <id>`: a plan's steps and its deviations (FL-155).

Nothing read `Plans`, `Steps` or `Deviations`. `metrics plan <id>` answers what
a plan COST in tool calls; `review evidence-log` answers what became of each
evidence slot; the dashboard's `/plan/{id}` is HTML over HTTP and so is no use
to a model in a terminal. The rows that say what actually happened while the
plan ran had no reader at all, and the only way to them was hand-written SQL.

The case that made this a release blocker, because it is the argument: a person
asked why a timeout had the value it had. The honest answer was two halves, and
both come out of these tables. The first half was recorded precisely, with the
number: a recovery step under the review FAILED with "timed out: exceeded the
ceiling", and the deviation that answered it names the ceiling it raised to.

The second half was recorded too, and finding that out is why this read exists. An
earlier version of this docstring asserted that the chosen value "is not recorded
anywhere" — written after searching `change_reason`, `utterance` and
`Steps.failure_reason`, none of which held it. It was in `Steps.log_context`, on
the step that recorded the re-run: the measured duration of every earlier refresh,
the ceiling read off them, and a test that asserts the margin over the slowest
recorded run rather than the literal.

That mistake is the argument for this read, better than any example could be: three
separate searches concluded "not recorded" from their own coverage, and the record
was one unsearched column away. A read that makes a column reachable is worth more
than a conclusion drawn from the columns that already were.

Two properties of that plan are the reason the read is shaped the way it is:

1. **The plan's status is `COMPLETED`** and it contains three `FAILED` steps,
   because every failure was recovered. The detour is therefore invisible from
   the plan's own status, and these rows are the only thing that remembers it.
   So the failure count sits in the header of every plan, not only of failed
   ones: a read that showed failures only for FAILED plans would miss the
   entire case it exists for.
2. **The recovery is nested** — `REVIEW.1.1` retries `REVIEW.1`, and
   `REVIEW.1.1.1` retries that. `execution_order` reaches 19000000 to encode it
   and `depth_level` is stored, but a flat list still loses which attempt a
   retry retries. Steps come out in tree order with the depth they sit at, and
   the human form indents them.

`failure_reason` is printed whole, always. It carries two quite different kinds
of content — a timer's output above, and elsewhere a person's written account
of a process violation ("a COMMAND step completes only through run-step.sh,
which captures the exit code itself") — and neither is formatted as though it
were the other. `log_context` runs to 16 KiB per step, so it IS bounded; but
per FL-154 the bound announces itself and names the command that lifts it,
because introducing a second silent truncation while fixing the first would be
absurd.

Read-only: SELECTs only. No `read_hit`, no counter, no model call.
"""
from __future__ import annotations

import json

CAP_LOG = 240             # characters of a step's captured output that travel by default
PLAN_COLUMNS = ("plan_id", "status", "project", "project_source", "original_goal", "user_query",
                "revision_count", "max_revisions", "created_at", "completed_at", "updated_at",
                "review_state", "review_skip_reason", "session_id")
STEP_COLUMNS = ("step_id", "parent_step_id", "description", "status", "execution_order", "depth_level",
                "step_type", "is_review", "attempt_count", "started_at", "completed_at", "updated_at",
                "failure_reason", "summary", "log_context", "agent_output")
# What gets bounded, and the prefix its size / cut are reported under.
# `description` is the instruction the step was GIVEN (on the live plan they run
# past 1 KiB each, and 29 of them swamp the read); `log_context` and
# `agent_output` are captured output, up to 16 KiB per step. All three are
# bounded and all three say what they cut.
# `failure_reason` and `summary` are never bounded: they are short accounts
# written to be read, and cutting a one-line account is the defect, not the
# bound — the 300.1s that answers the timeout question lives in one of them.
BOUNDED_FIELDS = {"description": "description", "log_context": "log", "agent_output": "agent_output"}


def _cut(text: str | None, cap: int | None) -> tuple[str, int, int]:
    """(what travels, how long it really is, how much is missing)."""
    t = text or ""
    if not cap or cap <= 0 or len(t) <= cap:
        return t, len(t), 0
    return t[:cap], len(t), len(t) - cap


def _tree(rows: list[dict]) -> list[dict]:
    """The steps in tree order, each carrying the depth it sits at.

    Depth is recomputed from the parent chain rather than read from
    `depth_level`: the column is what the writer believed, and this read is the
    one place that can disagree with it out loud. Children are ordered by
    `execution_order` then id — the same order they ran in. A step whose parent
    is not in this plan (or a cycle) is not dropped: it comes out at the end,
    at depth 0, because losing a row would be worse than mis-indenting one."""
    by_parent: dict[str | None, list[dict]] = {}
    ids = {r["step_id"] for r in rows}
    for r in rows:
        parent = r["parent_step_id"] if r["parent_step_id"] in ids else None
        by_parent.setdefault(parent, []).append(r)
    for kids in by_parent.values():
        kids.sort(key=lambda r: (r["execution_order"] if r["execution_order"] is not None else 0, r["step_id"]))
    out: list[dict] = []
    seen: set[str] = set()

    def walk(parent: str | None, depth: int) -> None:
        for r in by_parent.get(parent, ()):
            if r["step_id"] in seen:
                continue
            seen.add(r["step_id"])
            out.append({**r, "depth": depth})
            walk(r["step_id"], depth + 1)

    walk(None, 0)
    for r in rows:                                  # a cycle leaves rows unvisited; they still get printed
        if r["step_id"] not in seen:
            out.append({**r, "depth": 0})
    return out


def plan(conn, plan_id: str, *, log_chars: int | None = CAP_LOG, step: str | None = None) -> dict | None:
    """One plan, its steps in tree order, and its deviations. None when there is
    no such plan — an id that names nothing is not an empty plan.

    `log_chars=0` lifts the bound on captured output. `step` narrows the step
    list to one id while every count still describes the whole plan, so a
    narrowed read never misrepresents what the plan contains."""
    row = conn.execute(f"SELECT {', '.join(PLAN_COLUMNS)} FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    if row is None:
        return None
    doc: dict = dict(zip(PLAN_COLUMNS, row))
    raw = [dict(zip(STEP_COLUMNS, r)) for r in conn.execute(
        f"SELECT {', '.join(STEP_COLUMNS)} FROM Steps WHERE plan_id = ? ORDER BY execution_order, step_id", (plan_id,))]
    steps = _tree(raw)
    by_status: dict[str, int] = {}
    for s in steps:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
    doc["counts"] = {"steps": len(steps), "by_status": by_status, "failed": by_status.get("FAILED", 0),
                     "deviations": conn.execute("SELECT COUNT(*) FROM Deviations WHERE plan_id = ?", (plan_id,)).fetchone()[0]}
    doc["failed"] = [s["step_id"] for s in steps if s["status"] == "FAILED"]
    doc["step"] = step
    shown = [s for s in steps if step is None or s["step_id"] == step]
    for s in shown:
        for field, prefix in BOUNDED_FIELDS.items():
            text, total, cut = _cut(s.get(field), log_chars)
            s[field], s[f"{prefix}_chars"], s[f"{prefix}_cut"] = text, total, cut
        s["reads_whole"] = f"provledger plan {plan_id} --step {s['step_id']} --full"
    doc["steps"] = shown
    doc["deviations"] = [_deviation(r) for r in conn.execute(
        "SELECT deviation_id, target_step_id, justification, new_step_ids, revision_count, created_at "
        "FROM Deviations WHERE plan_id = ? ORDER BY deviation_id", (plan_id,))]
    doc["headline"] = f"provledger headline show {plan_id}"
    ctx = conn.execute("SELECT LENGTH(impact_context) FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    doc["impact_context_chars"] = int(ctx[0] or 0) if ctx else 0
    return doc


def _deviation(r) -> dict:
    try:
        new_ids = json.loads(r[3]) if r[3] else []
    except ValueError:
        new_ids = []
    return {"deviation_id": r[0], "target_step_id": r[1], "justification": r[2],
            "new_step_ids": new_ids if isinstance(new_ids, list) else [], "revision_count": r[4], "created_at": r[5]}


# ── rendering ────────────────────────────────────────────────────────────────

def render(doc: dict | None, plan_id: str | None = None) -> str:
    if doc is None:
        return f"{plan_id or '(plan)'}: no such plan in the ledger"
    c = doc["counts"]
    project = doc.get("project") or "(no project)"
    head = (f"{doc['plan_id']} · {doc['status']} · {project}"
            + (f" ({doc['project_source']})" if doc.get("project_source") else "")
            + f" · steps {c['steps']} · {c['failed']} failed · deviations {c['deviations']}"
            + f" · revisions {doc.get('revision_count')}/{doc.get('max_revisions')}")
    lines = [head]
    if c["failed"] and doc["status"] == "COMPLETED":
        lines.append("   the plan closed COMPLETED: the failures below were recovered, and nothing but these rows remembers them")
    if doc.get("original_goal"):
        lines.append(f"   goal: {doc['original_goal']}")
    if doc.get("user_query"):
        lines.append(f"   asked: {doc['user_query']}")
    lines.append(f"   created {doc.get('created_at')}"
                 + (f" · completed {doc['completed_at']}" if doc.get("completed_at") else " · not completed")
                 + (f" · review {doc['review_state']}" if doc.get("review_state") else ""))
    if doc.get("review_skip_reason"):
        lines.append(f"   review skipped: {doc['review_skip_reason']}")
    lines.append("")
    if doc.get("step") and not doc["steps"]:
        lines.append(f"── step {doc['step']}: no step with that id in this plan")
    else:
        label = f"── step {doc['step']}" if doc.get("step") else f"── steps · {len(doc['steps'])} · in tree order, a child indented under the step it hangs on"
        lines.append(label)
        for s in doc["steps"]:
            lines += _step_lines(s)
    if doc["failed"] and not doc.get("step"):
        lines += ["", f"── failed · {len(doc['failed'])} · " + " · ".join(doc["failed"])]
    if doc["deviations"]:
        lines += ["", f"── deviations · {len(doc['deviations'])}"]
        for d in doc["deviations"]:
            # `#v`, not a bare `#N`: deviations and ledger records numbered from
            # separate sequences, and both printed as `#94`. A reader handed
            # "deviation #94" and told to check it with `provledger record '#94'`
            # got an unrelated reason from another plan, with no error — a silent
            # wrong answer in the one place this tool must be checkable. The rest
            # of the namespace already separates kinds by letter (`#r` a source,
            # `#i` an influence, `#x` an expectation, `#m` a measured value).
            lines.append(f"   #v{d['deviation_id']} · {d['created_at']} · against {d['target_step_id'] or '(the plan itself)'}"
                         + (f" · inserted {', '.join(d['new_step_ids'])}" if d["new_step_ids"] else "")
                         + f" · revision {d['revision_count']}")
            lines.append(f"      {d['justification']}")
    lines += ["", "── how to go on",
              f"   {doc['headline']}                        the two-layer check this plan was published with",
              f"   provledger plan {doc['plan_id']} --step <step_id> --full   one step, captured output and all",
              "   provledger why <node>                                 that node's history, bounded",
              "   provledger record #<id>                               one record, whole and uncut"]
    return "\n".join(lines)


def _step_lines(s: dict) -> list[str]:
    pad = "   " * (s["depth"] + 1)
    inner = pad + "   "
    head = (f"{pad}{s['step_id']} · {s['status']}"
            + (f" · {s['step_type']}" if s.get("step_type") else "")
            + (" · review" if s.get("is_review") else "")
            + (f" · attempt {s['attempt_count']}" if s.get("attempt_count") else ""))
    out = [head]
    if s.get("description"):
        out.append(f"{inner}{s['description']}")
        if s.get("description_cut"):
            out.append(f"{inner}… [+{s['description_cut']} chars cut of {s.get('description_chars')} — `{s['reads_whole']}` reads it whole]")
    if s.get("failure_reason"):
        # whole, always: the number that answers the question lives in here
        out.append(f"{inner}failed: {s['failure_reason']}")
    if s.get("summary"):
        out.append(f"{inner}summary: {s['summary']}")
    for field, prefix in BOUNDED_FIELDS.items():
        if field == "description":
            continue                       # already on its own line above, unlabelled: it heads the step
        text = s.get(field) or ""
        cut = s.get(f"{prefix}_cut") or 0
        if not text and not cut:
            continue
        out.append(f"{inner}{prefix}: " + text.replace("\n", " ⏎ "))
        if cut:
            out.append(f"{inner}… [+{cut} chars cut of {s.get(f'{prefix}_chars')} — `{s['reads_whole']}` reads it whole]")
    return out
