"""external_trigger — the judge for changes that land outside the code
(DP phase 5, Task 0; spec §4, design doc §5.2 and D8; C3–C5).

R0–R6 answer for code, from the ledger and the graph. Nothing answers for the
deck: a number on slide 4 can change with no line of code and no line of data
behind it, and the reason for that change is almost never anywhere a rule can
reach. This is the second path — a headless model, given the five paired
examples verbatim, asked two questions in order:

  1. is the change ODD (a number moved, a conclusion removed, nothing in the
     data accounting for it);
  2. is the reason ALREADY IN THE PERSON'S WORDS.

Odd is not the same as ask, and keeping the two apart is what removes most of
the questions: an odd change whose reason is in the sentence is recorded as a
`stated` reason (rule X1, the span the person actually said) and nobody is
asked anything.

**The disposition is D8, and it is in the prompt with its argument.** A missed
record costs one record and can be added later. One needless question teaches
the person to skip every question after it, which kills the feature silently
and cannot be undone. So everything ambiguous ends in `silent`: an answer that
is not the strict JSON object, a span the utterance does not contain, a runner
that returned nothing. None of them ever becomes an ask.

Three verdicts, and every one of them is a `trigger_log` row on path
`external` (C4) — including the ones taken while the switch is off, so the
false-ask and miss rates have a denominator (C5).

  auto    the reason was in the words → a stated reason, rule_id X1
  ask     odd, and the reason is nowhere in the words → a close-time slot
  silent  not odd, or nothing could be told from the answer

Nothing here runs a model on its own: `runner` is injected, exactly as the
phase-8 arbiter injects one, and every test passes a stub.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import provenance, triggers

RULE_ID = "X1"
PATH = "external"
PROMPT_PATH = Path(__file__).resolve().parent / "testing" / "prompts" / "external_trigger.md"
META_RULE = "不确定时，不问。"
_JSON_RE = re.compile(r"\{.*\}", re.S)
BASIS_MAX = 400

# The words that say a person is talking about something outside the code. Two
# lists because the person is: an English deck and a Chinese one are the same
# artifact, and a judge that only knows one of them is silently half-blind.
ARTIFACT_WORDS_EN = ("deck", "slide", "slides", "presentation", "sheet", "spreadsheet", "workbook",
                     "report", "chart", "appendix", "pptx", "xlsx", "docx", "csv")
ARTIFACT_WORDS_ZH = ("幻灯", "演示", "报表", "报告", "表格", "图表", "工作表", "附录", "这页", "那页")

# A node whose identity is a data point rather than a symbol (DP phase 4, §9).
EXTERNAL_NODE_TYPES = frozenset({"metric", "manual_figure", "declared"})
EXTERNAL_PREFIXES = ("metric:", "declared:")


@dataclass(frozen=True)
class Verdict:
    """One judgement. `reason` is the span to record, already checked against
    the utterance it points at; it is never set unless `verdict` is 'auto'."""
    node_key: str
    verdict: str                                  # auto | ask | silent
    trigger: bool
    basis: str
    reason: tuple[int, int, int] | None = None    # (utterance_id, start, end)
    raw: str | None = None


# ── is this the external path at all ─────────────────────────────────────────

def mentions_artifact(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(w in low for w in ARTIFACT_WORDS_EN) or any(w in text for w in ARTIFACT_WORDS_ZH)


def _known_files(conn, project: str) -> set[str]:
    rows = conn.execute("SELECT DISTINCT path FROM artifact_file WHERE project = ?", (project,)).fetchall()
    return {r[0] for r in rows}


def is_external_node(conn, project: str, node: dict) -> bool:
    """The node is a data point in an external artifact: its identity says so,
    its type says so, it has an occurrence, or the plan changed it inside a
    file the ledger already knows as an artifact."""
    key = node.get("node_key") or ""
    qn = node.get("qualified_name") or ""
    if any(key.startswith(p) or qn.startswith(p) for p in EXTERNAL_PREFIXES):
        return True
    if (node.get("node_type") or "") in EXTERNAL_NODE_TYPES:
        return True
    if key and conn.execute("SELECT 1 FROM occurrence WHERE project = ? AND node_key = ? LIMIT 1",
                            (project, key)).fetchone():
        return True
    fp = node.get("file_path") or ""
    return bool(fp and fp in _known_files(conn, project))


def is_external_change(ctx, node: dict) -> bool:
    """An external node AND a plan whose words are about the artifact. Both
    halves are required: a metric named in a sentence about `compute_conversion`
    is a code change, and a sentence about a deck that names a function is not
    licence to judge the function."""
    if not is_external_node(ctx.conn, ctx.project, node):
        return False
    return any(mentions_artifact(u["text"]) for u in triggers.candidate_utterances(ctx))


def candidates(ctx) -> list[dict]:
    """The external nodes this plan may have changed, as node dicts.

    A deck's numbers are in the orchestrator database, not in any snapshot, so
    the analyzer never reports them as touched. What narrows them here is the
    person's own words: a node is a candidate when one of the plan's artifact
    sentences names the place it was anchored to, the file it turned up in, or
    the node itself — plus any node the plan touched inside a known artifact
    file. Nothing is judged because it merely exists."""
    conn, project = ctx.conn, ctx.project
    said = [u for u in triggers.candidate_utterances(ctx) if mentions_artifact(u["text"])]
    out: dict[str, dict] = {}
    if said:
        rows = conn.execute(
            "SELECT o.node_key, o.locator_json, f.path FROM occurrence o JOIN artifact_file f ON f.id = o.file_id "
            "WHERE o.project = ? ORDER BY o.id", (project,)).fetchall()
        for node_key, locator_json, path in rows:
            try:
                at = (json.loads(locator_json or "{}") or {}).get("at") or ""
            except ValueError:
                at = ""
            names = [n for n in (at, Path(path).name, path, node_key, node_key.split(":", 1)[-1]) if n]
            if any(any(n.lower() in u["text"].lower() for n in names) for u in said):
                out.setdefault(node_key, {"node_key": node_key, "qualified_name": node_key, "node_type": "metric",
                                          "file_path": path, "event_types": [], "payloads": [], "run_id": None})
    for key, node in ctx.touched.items():
        if key not in out and is_external_node(conn, project, node):
            out[key] = node
    return [out[k] for k in sorted(out)]


# ── the prompt and the answer ────────────────────────────────────────────────

def prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _occurrences(conn, project: str, node_key: str) -> list[dict]:
    rows = conn.execute(
        "SELECT o.locator_json, o.value_text, o.seen_at, f.path, f.kind FROM occurrence o "
        "JOIN artifact_file f ON f.id = o.file_id WHERE o.project = ? AND o.node_key = ? ORDER BY o.id",
        (project, node_key)).fetchall()
    out = []
    for locator_json, value_text, seen_at, path, kind in rows:
        try:
            locator = json.loads(locator_json or "{}")
        except ValueError:
            locator = {}
        out.append({"file": path, "kind": kind, "at": locator.get("at"), "value": value_text, "seen_at": seen_at})
    return out


def payload(ctx, node: dict) -> dict:
    """What the judge is shown: the node, where it has turned up, and the
    sentences — each with the id and the exact text a span is measured in.

    Every sentence of the plan, not only the ones naming the deck: the fifth
    example ("转化率改成 2.8%，Sam 说 EMEA 不算在 Q3 里") carries its reason in a
    sentence that never says "slide". Filtering the material the way the path
    is decided would hide exactly the sentences this judge exists to find."""
    said = triggers.candidate_utterances(ctx)
    return {"node": {"node_key": node.get("node_key"), "name": node.get("qualified_name"),
                     "node_type": node.get("node_type"), "file": node.get("file_path")},
            "occurrences": _occurrences(ctx.conn, ctx.project, node.get("node_key") or ""),
            "utterances": [{"utterance_id": u["id"], "text": u["text"]} for u in said]}


def prompt_for(ctx, node: dict) -> str:
    return prompt_text().rstrip("\n") + "\n\n" + json.dumps(payload(ctx, node), ensure_ascii=False, indent=1,
                                                            sort_keys=True) + "\n"


def parse_answer(text: str | None) -> dict | None:
    """The strict answer shape, or None. A code fence around the object is
    tolerated; anything else — prose, a missing key, a wrong type — is None,
    and None means silent."""
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        doc = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("trigger"), bool) or not isinstance(doc.get("basis"), str):
        return None
    reason = doc.get("reason_in_utterance")
    if reason is not None:
        if not isinstance(reason, dict):
            return None
        span = reason.get("span")
        if not (isinstance(reason.get("utterance_id"), int) and isinstance(span, list) and len(span) == 2
                and all(isinstance(x, int) for x in span)):
            return None
    return {"trigger": doc["trigger"], "reason_in_utterance": reason, "basis": doc["basis"].strip()[:BASIS_MAX]}


# ── the judgement ────────────────────────────────────────────────────────────

def judge(ctx, node: dict, *, runner=None, model: str | None = None) -> Verdict:
    """Ask once, decide once. Writes nothing: `evaluate_external` records."""
    key = node.get("node_key") or ""
    if runner is None:
        from .testing.claude_arbiter import default_runner
        runner = default_runner
    raw = runner(prompt_for(ctx, node), model=model)
    doc = parse_answer(raw)
    if doc is None:
        return Verdict(key, "silent", False, "unparseable answer — nothing was asked", None, raw)
    basis = doc["basis"] or "no basis given"
    if not doc["trigger"]:
        return Verdict(key, "silent", False, basis, None, raw)
    reason = doc["reason_in_utterance"]
    if reason is None:
        return Verdict(key, "ask", True, basis, None, raw)
    span = (int(reason["utterance_id"]), int(reason["span"][0]), int(reason["span"][1]))
    try:
        checked = provenance._check_span(ctx.conn, span)
    except ValueError as e:
        # The reason it claims is not in the sentence it points at, so there is
        # nothing to record — and nothing to ask about either (D8).
        return Verdict(key, "silent", True, f"{basis} · discarded: the span is not in that utterance ({e})", None, raw)
    return Verdict(key, "auto", True, basis, checked, raw)


def _already_judged(conn, plan_id: str, node_key: str) -> bool:
    return conn.execute("SELECT 1 FROM trigger_log WHERE plan_id = ? AND node_key = ? AND path = ? LIMIT 1",
                        (plan_id, node_key, PATH)).fetchone() is not None


def _log(conn, *, project: str, plan_id: str, node_key: str, rule_id: str | None, verdict: str, basis: str) -> int:
    cur = conn.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, rule_id, verdict, basis) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (project, plan_id, node_key, PATH, rule_id, verdict, (basis or "")[:BASIS_MAX]))
    return int(cur.lastrowid)


OFF_BASIS = "external trigger off — no model was asked"


def evaluate_external(conn, *, project: str, plan_id: str, psg_db_path: str | None = None, runner=None,
                      model: str | None = None, mode: str = "on", ctx=None, commit: bool = False) -> dict:
    """Judge every external candidate of this plan exactly once.

    With `mode='off'` no model is called and every candidate still gets its
    row, basis `external trigger off`: the rates in C5 need a denominator, and
    a switch that also switches off the record would make the switch
    unmeasurable — which is the one thing this module exists to prevent.

    `ctx` is the caller's own context when it has one — `triggers.evaluate`
    already built it, and building it twice means re-reading the graph, the
    steps and the deviations for the same plan (H2: retrieve once).
    """
    ctx = ctx if ctx is not None else triggers._ctx(conn, project, plan_id, psg_db_path)
    out = {"plan_id": plan_id, "mode": mode, "judged": 0, "auto": 0, "ask": 0, "silent": 0, "nodes": []}
    for node in candidates(ctx):
        key = node["node_key"]
        if _already_judged(conn, plan_id, key) or triggers._has_rule_reason(conn, plan_id, key):
            continue
        if mode != "on":
            _log(conn, project=project, plan_id=plan_id, node_key=key, rule_id=None, verdict="silent", basis=OFF_BASIS)
            out["judged"] += 1
            out["silent"] += 1
            out["nodes"].append({"node_key": key, "verdict": "silent", "basis": OFF_BASIS})
            continue
        v = judge(ctx, node, runner=runner, model=model)
        reason_id = None
        if v.verdict == "auto":
            reason_id = provenance.insert_reason(conn, project=project, plan_id=plan_id, node_key=key,
                                                 kind="organizational", verbatim=v.reason, rule_id=RULE_ID,
                                                 recorded_by="system", commit=False)
        _log(conn, project=project, plan_id=plan_id, node_key=key,
             rule_id=RULE_ID if v.verdict == "auto" else None, verdict=v.verdict, basis=v.basis)
        out["judged"] += 1
        out[v.verdict] += 1
        out["nodes"].append({"node_key": key, "verdict": v.verdict, "basis": v.basis, "reason_id": reason_id})
    if commit:
        conn.commit()
    return out


# ── the two rates the judge is measured by (C5) ──────────────────────────────
#
# An LLM verdict's problem is not that it is sometimes wrong; it is that when it
# is wrong nobody finds out. These two numbers are what the log is for, and both
# are computed from what actually happened afterwards, never from a self-report:
#
#   false ask   the judge asked, the person answered, and the answer was
#               `unstated` — they had nothing to say, so the question cost
#               trust and bought nothing
#   miss        the judge stayed silent and the person came back on their own
#               and recorded a reason — the change was odd after all
#
# Both print their denominator. A rate over two answers is not the same
# statement as a rate over two hundred, and rounding that difference away is how
# a calibration number starts lying.

_RATE_SQL_ASKS = """
SELECT t.node_key,
       (SELECT COUNT(*) FROM change_reason r WHERE r.plan_id = t.plan_id AND r.node_key = t.node_key
          AND r.role = 'reason' AND r.state = 'active' AND r.superseded_by IS NULL) AS answers,
       (SELECT COUNT(*) FROM change_reason r WHERE r.plan_id = t.plan_id AND r.node_key = t.node_key
          AND r.role = 'reason' AND r.tier = 'unstated' AND r.state = 'active' AND r.superseded_by IS NULL) AS unstated
FROM trigger_log t WHERE t.path = 'external' AND t.verdict = 'ask'
"""

_RATE_SQL_SILENCES = """
SELECT t.node_key,
       (SELECT COUNT(*) FROM change_reason r WHERE r.plan_id = t.plan_id AND r.node_key = t.node_key
          AND r.role = 'reason' AND r.recorded_by = 'human' AND r.tier <> 'unstated'
          AND r.state = 'active' AND r.superseded_by IS NULL) AS volunteered
FROM trigger_log t WHERE t.path = 'external' AND t.verdict = 'silent'
"""


def rates(conn, project: str | None = None) -> dict:
    """{asks, asks_answered, false_asks, external_false_ask_rate,
        silences, misses, external_miss_rate} — the rates are None, not 0.0,
    when nothing has been answered yet: never asked is not the same statement
    as never wrong."""
    where = " AND t.project = ?" if project else ""
    args: tuple = (project,) if project else ()
    asks = conn.execute(_RATE_SQL_ASKS + where, args).fetchall()
    silences = conn.execute(_RATE_SQL_SILENCES + where, args).fetchall()
    answered = [r for r in asks if r[1]]
    false_asks = sum(1 for r in answered if r[2])
    misses = sum(1 for r in silences if r[1])
    return {"asks": len(asks), "asks_answered": len(answered), "false_asks": false_asks,
            "external_false_ask_rate": round(false_asks / len(answered), 4) if answered else None,
            "silences": len(silences), "misses": misses,
            "external_miss_rate": round(misses / len(silences), 4) if silences else None}
