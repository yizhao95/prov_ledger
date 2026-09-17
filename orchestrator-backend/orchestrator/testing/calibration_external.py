"""calibration_external — the calibration set for the external-artifact judge
and the gate it must clear before it may ask anyone anything (DP phase 5,
Task 2; spec §4, design doc §5.2, C3/C5).

Two kinds of truth, and only two:

  · **the five paired examples** of design doc §5.2, whose right answer is
    known without anybody labelling anything because the design document
    states it. They are re-runnable, so they are what consistency is measured
    on: the same judge, the same sentence, three runs, the same verdict.
  · **a person's mark** on a verdict that really happened — `right` or
    `wrong`. A mark is an APPENDED trigger_log row pointing at the row it
    marks, never an update of it: `trigger_log` refuses UPDATE, and a
    calibration set that can rewrite its own history calibrates nothing.

The bar is phase 7's, imported rather than restated: consistency exactly 1.0
(a judge that wavers is out), accuracy ≥ 0.9, at least 10 labelled items. Five
seeds are therefore never enough on their own — switching this on takes real
verdicts that a real person has looked at. Failing the gate is a legitimate
outcome; the switch simply stays off (`reasons.external_trigger`).

Nothing here runs a model on its own: `runner` is injected, the same way the
phase-8 arbiter's is.
"""
from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import db, external_trigger, provenance, triggers
from . import calibration

VERSION = 1
REPORT_ID = "external_trigger"
MIN_TRUTH = calibration.MIN_TRUTH          # 10 — phase 7's number, unchanged
MIN_ACCURACY = calibration.MIN_ACCURACY    # 0.9 — phase 7's number, unchanged
LABELS = ("right", "wrong")
LABEL_RULE_ID = "X1-label"
PROJECT = "calibration"
PLAN = "calib-external"
DECK = "decks/q3.pptx"


@dataclass(frozen=True)
class Example:
    """One of the five pairs. `reason` is the fragment of the sentence that
    already carries the why, when there is one."""
    id: str
    utterance: str
    node_key: str
    at: str
    value: str
    trigger: bool
    reason: str | None
    verdict: str            # silent | ask | auto


def examples() -> tuple[Example, ...]:
    """The five paired examples, verbatim from design doc §5.2. Odd is not the
    same as ask: three of the five are odd, but only two become questions."""
    return (
        Example("correction", "slide 4 转化率写成了 3.02%，应该是 3.2%",
                "metric:q3_conv", "slide 4", "3.2", False, None, "silent"),
        Example("bare_change", "slide 4 转化率改成 2.8%",
                "metric:q3_conv", "slide 4", "3.2", True, None, "ask"),
        Example("sync", "把 slide 4 按最新一轮结果更新",
                "metric:q3_conv", "slide 4", "3.2", False, None, "silent"),
        Example("deleted_conclusion", "把 slide 7 关于 EMEA 增长那段去掉",
                "declared:emea-growth", "slide 7", "EMEA growth", True, None, "ask"),
        Example("reason_in_the_sentence", "转化率改成 2.8%，Sam 说 EMEA 不算在 Q3 里",
                "metric:q3_conv", "slide 4", "3.2", True, "Sam 说 EMEA 不算在 Q3 里", "auto"),
    )


# ── a person's mark, appended ────────────────────────────────────────────────

def label(conn, trigger_log_id: int, user_action: str, *, by: str = "human",
          note: str | None = None, commit: bool = True) -> int:
    """Mark a verdict `right` or `wrong`. Appends a row carrying `user_action`
    and naming the row it marks; the judged row is never touched."""
    if user_action not in LABELS:
        raise ValueError(f"a label is right or wrong, got {user_action!r}")
    row = conn.execute("SELECT project, plan_id, node_key, path, verdict FROM trigger_log WHERE id = ?",
                       (trigger_log_id,)).fetchone()
    if row is None:
        raise ValueError(f"trigger_log {trigger_log_id} does not exist")
    project, plan_id, node_key, path, verdict = row
    basis = f"label of trigger_log #{trigger_log_id} by {by}" + (f": {note}" if note else "")
    cur = conn.execute(
        "INSERT INTO trigger_log (project, plan_id, node_key, path, rule_id, verdict, basis, user_action) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project, plan_id, node_key, path, LABEL_RULE_ID, verdict, basis[:400], user_action))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def labelled(conn, project: str | None = None) -> list[dict]:
    """Every marked verdict: {id, marks, verdict, user_action, basis}."""
    sql = ("SELECT id, project, plan_id, node_key, verdict, user_action, basis FROM trigger_log "
           "WHERE rule_id = ? AND user_action IN (?, ?)")
    args: list = [LABEL_RULE_ID, *LABELS]
    if project:
        sql += " AND project = ?"
        args.append(project)
    return [{"id": r[0], "project": r[1], "plan_id": r[2], "node_key": r[3], "verdict": r[4],
             "user_action": r[5], "basis": r[6]} for r in conn.execute(sql + " ORDER BY id", args)]


# ── replaying one example ────────────────────────────────────────────────────

def _scaffold(conn, ex: Example):
    """The smallest ledger an example needs: the plan, its sentence, and the
    place the number turned up. Built per example so no example can see
    another's words."""
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) "
                 "VALUES (?, 'calibration', 'IN_PROGRESS', ?, 'declared', '2026-09-17 09:00:00')", (PLAN, PROJECT))
    uid = provenance.insert_utterance(conn, session_id="calibration", project=PROJECT, plan_id=PLAN,
                                      text=ex.utterance, occurred_at="2026-09-17 09:10:00", commit=False)
    from ..artifacts import anchor
    file_id = anchor.register_file(conn, PROJECT, DECK, "sha-calibration", "pptx", commit=False)
    provenance._insert_chained(conn, "occurrence", {
        "project": PROJECT, "node_key": ex.node_key, "file_id": file_id,
        "locator_json": json.dumps({"at": ex.at, "kind": "pptx"}, sort_keys=True, ensure_ascii=False),
        "value_text": ex.value, "value_num": None, "seen_at": "2026-09-16 12:00:00",
        "tier": "observed", "by": "human", "recorded_at": "2026-09-16 12:00:00"})
    conn.commit()
    ctx = triggers._ctx(conn, PROJECT, PLAN, None)
    node = {"node_key": ex.node_key, "qualified_name": ex.node_key, "node_type": "metric",
            "file_path": DECK, "event_types": [], "payloads": [], "run_id": None}
    return ctx, node, uid


@contextmanager
def example_ledger(ex: Example):
    """(ctx, node) for one example, in a throwaway database that is built once
    and reused for every run of that example — the ledger is the same material
    each time, and rebuilding it per run would only cost migrations."""
    with tempfile.TemporaryDirectory() as td:
        conn = db.open_db(Path(td) / "calibration.db")
        try:
            db.run_migrations(conn)
            ctx, node, _uid = _scaffold(conn, ex)
            yield ctx, node
        finally:
            conn.close()


def judge_example(ex: Example, runner, *, model: str | None = None) -> external_trigger.Verdict:
    """One replay of one example against `runner`, in a throwaway database."""
    with example_ledger(ex) as (ctx, node):
        return external_trigger.judge(ctx, node, runner=runner, model=model)


# ── the report and the gate ──────────────────────────────────────────────────

@dataclass
class ExternalReport:
    runner: str
    n_runs: int
    n_items: int
    n_truth: int
    consistency: float
    accuracy: float | None
    created_at: str
    items: list[dict] = field(default_factory=list)
    labels: int = 0
    unanswered: int = 0
    version: int = VERSION

    @property
    def ok(self) -> bool:
        return gate_reason(self) is None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, sort_keys=True, ensure_ascii=False, default=str)


def report_path(out_dir=None) -> Path:
    return (Path(out_dir) if out_dir else calibration.eval_dir()) / f"{REPORT_ID}.json"


def run(runner, *, n_runs: int = 3, conn=None, project: str | None = None, out_dir=None,
        model: str | None = None) -> ExternalReport:
    """Replay the five seeds n_runs times, add every marked verdict, write the
    report and return it. Consistency is measured on the seeds alone — they are
    the only items that can be asked again."""
    items: list[dict] = []
    consistent = correct = 0
    seeds = examples()
    unanswered = 0
    for ex in seeds:
        with example_ledger(ex) as (ectx, enode):
            judged = [external_trigger.judge(ectx, enode, runner=runner, model=model)
                      for _ in range(max(1, n_runs))]
        verdicts = [v.verdict for v in judged]
        silent_for_want_of_an_answer = all(v.basis.startswith(external_trigger.NO_ANSWER) for v in judged)
        unanswered += silent_for_want_of_an_answer
        is_consistent = all(v == verdicts[0] for v in verdicts)
        consistent += is_consistent
        is_correct = is_consistent and verdicts[0] == ex.verdict
        correct += is_correct
        items.append({"id": ex.id, "kind": "example", "answer": verdicts[0], "truth": ex.verdict,
                      "answers_distinct": len(set(verdicts)), "correct": is_correct,
                      "answered": not silent_for_want_of_an_answer})
    marks = labelled(conn, project) if conn is not None else []
    for m in marks:
        right = m["user_action"] == "right"
        correct += right
        items.append({"id": f"trigger_log#{m['id']}", "kind": "label", "answer": m["verdict"],
                      "truth": "right" if right else "wrong", "answers_distinct": 1, "correct": right})
    n_truth = len(seeds) + len(marks)
    rep = ExternalReport(runner=getattr(runner, "__name__", type(runner).__name__), n_runs=n_runs,
                         n_items=len(seeds), n_truth=n_truth,
                         consistency=round(consistent / len(seeds), 4) if seeds else 1.0,
                         accuracy=round(correct / n_truth, 4) if n_truth else None,
                         created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         items=items, labels=len(marks), unanswered=unanswered)
    p = report_path(out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(rep.to_json() + "\n", encoding="utf-8")
    return rep


def load_report(out_dir=None) -> ExternalReport | None:
    p = report_path(out_dir)
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    d.pop("version", None)
    return ExternalReport(**d)


def gate_reason(rep: ExternalReport | None, *, min_truth: int = MIN_TRUTH,
                min_accuracy: float = MIN_ACCURACY) -> str | None:
    """None when the report clears the bar, else the refusal — in words that
    say which number fell short and by how much."""
    if rep is None:
        return "no evaluation report"
    if rep.unanswered:
        return (f"the runner returned no answer for {rep.unanswered} of {rep.n_items} example(s) — "
                "an unreachable, refused or timed-out model is not a consistent judge, however "
                "consistently it says nothing")
    if rep.consistency < 1.0:
        return f"consistency {rep.consistency:.3f} < 1.0 — a judge that wavers may not ask anybody anything"
    if rep.n_truth < min_truth:
        return (f"only {rep.n_truth} labelled item(s) (< {min_truth}); the five examples are seeds, "
                "not a calibration set — mark real verdicts with `provledger trigger label`")
    if rep.accuracy is None or rep.accuracy < min_accuracy:
        return f"accuracy {rep.accuracy if rep.accuracy is not None else 'n/a'} < {min_accuracy} on {rep.n_truth} labelled items"
    return None


def gate(out_dir=None) -> tuple[bool, str]:
    """(ok, detail) for switching `reasons.external_trigger` on."""
    rep = load_report(out_dir)
    reason = gate_reason(rep)
    if reason is None:
        return True, (f"passed (consistency {rep.consistency:.2f}, accuracy {rep.accuracy:.2f} "
                      f"on {rep.n_truth} labelled items)")
    return False, f"refused: {reason}"

# ── the stub every test and the CLI's dry run share ──────────────────────────

def truthful_runner():
    """A runner that answers each seed the way the design document labels it.
    It reads the MATERIAL, never the prompt: the prompt carries all five
    example sentences, so anything keyed on the prompt matches every time."""
    def runner(prompt, *, model=None, timeout_s=None):
        material = json.loads(prompt.rsplit("\n\n", 1)[-1])
        said = " ".join(u["text"] for u in material["utterances"])
        uid = material["utterances"][0]["utterance_id"] if material["utterances"] else 0
        for ex in examples():
            if ex.utterance in said:
                reason = None
                if ex.reason:
                    start = ex.utterance.index(ex.reason)
                    reason = {"utterance_id": uid, "span": [start, start + len(ex.reason)]}
                return json.dumps({"trigger": ex.trigger, "reason_in_utterance": reason,
                                   "basis": f"stub: {ex.id}"}, ensure_ascii=False)
        return "I could not tell."
    runner.__name__ = "stub-truthful"
    return runner
