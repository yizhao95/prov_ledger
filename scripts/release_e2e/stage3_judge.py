#!/usr/bin/env python3
"""stage3_judge — have a model mark the answers.

The dummy project's history was written by `dummy_project.py`, so the correct
answer to every question is known before the question is asked. That is what
makes this markable at all: each question carries a list of key points a right
answer must contain — a specific date, a named source, a decision that was
rejected, a mistake nobody asked about, or an absence that must be reported as
an absence rather than filled in.

**Why this is not the existing assertions.** Those check the *shape* of an
answer: that every sentence carries a citation, that the scope line is counted,
that the three sections are present in order. FL-145 is the proof that shape is
not enough — a `receipts` run whose format was perfect and whose answer was
wrong, with ten green tests behind it. A judge that asks "are there three
sections" reproduces exactly that blindness. So this one is told to ignore
formatting entirely and score two things:

  · did the answer hit each key point, and
  · the two failure modes that matter — a confident claim resting on no record,
    and a real recorded fact that was there and was missed.

**The judge is itself a model, so its verdict is evidence and not proof.** It
can misread an answer that is right, and it can wave through an answer that is
wrong. Every verdict is printed with the judge's own words next to it so a
person can check the judging. A disagreement between the judge and the answer
deserves a human's eye — not an automatic pass and not an automatic fail. The
exit code reflects the judge's verdict because a release gate has to say
something; what it says is "look at this", not "this is settled".

Where the model cannot be reached, this stage reports BLOCKED. It never passes
silently: a check that quietly skips is worse than one that fails.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_model as SM                                          # noqa: E402
from e2elib import (BLOCKED, FAIL, OK, RED, GRN, YEL, Z, ROOT,      # noqa: E402
                    Tally, banner, info, step, write_verdict)

MODEL = (os.environ.get("E2E_MODEL") or "").strip() or None

RUBRIC = """\
You are marking one answer produced by a provenance tool against key points
that were written down BEFORE the question was asked. The project's history was
constructed on purpose, so the key points are ground truth, not opinion.

Mark CONTENT, not form. Ignore formatting, section headings, section order,
citation style, tone and length completely. An answer with perfect formatting
and the wrong content fails; an answer that is right in plain prose passes. The
defect this marking exists to catch was a reply whose format was flawless and
whose substance was wrong, waved through by ten tests that only checked shape.

For each key point, decide:
  "hit"     — the answer actually conveys this point. Different wording is fine.
  "partial" — the point is gestured at but the specific thing (the date, the
              name, the number, the rejection) is not actually stated.
  "miss"    — the answer does not convey it.

Then judge two failure modes independently of the key points:
  unsupported_claim — the answer asserts something as recorded that the material
                      does not support (a cause, a date, a name, an approver, a
                      number). Quote it. This is the worst failure the tool can
                      make, because the claim gets forwarded as evidence.
  missed_fact       — the material plainly contains a relevant recorded fact and
                      the answer does not use it. Quote it.

If the question's key points say an absence must be reported as an absence, then
an answer that says plainly "there is no record of that" is CORRECT and must be
marked hit. An answer that supplies a plausible explanation instead is a miss AND
an unsupported_claim, however well written it is.

Reply with ONE JSON object and nothing else:

{"points": [{"point": "<the key point, copied>", "verdict": "hit|partial|miss",
             "evidence": "<the words in the answer that decide it, or why not>"}],
 "unsupported_claim": {"found": true|false, "quote": "<...>", "why": "<...>"},
 "missed_fact": {"found": true|false, "quote": "<...>", "why": "<...>"},
 "verdict": "pass|partial|fail",
 "note": "<one sentence for the person reading this>"}
"""


def judge_one(q: dict) -> tuple[dict, dict, str]:
    must = q.get("must") or []
    must_not = q.get("must_not") or []
    prompt = (
        RUBRIC
        + "\n===== the question that was asked =====\n" + q["question"]
        + "\n\n===== the key points a right answer must contain =====\n"
        + "\n".join(f"{i + 1}. {m}" for i, m in enumerate(must))
        + ("\n\n===== and what it must NOT do =====\n"
           + "\n".join(f"- {m}" for m in must_not) if must_not else "")
        + "\n\n===== the material the tool computed (the only admissible evidence) =====\n"
        + (q.get("material") or "(none)")[:24000]
        + "\n\n===== the answer to mark =====\n" + (q.get("answer") or "(empty)")
        + "\n"
    )
    text, detail, outcome = SM.call(prompt, model=MODEL)
    if outcome != "ok":
        return {}, detail, outcome
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}, {**detail, "reason": "the judge did not answer with JSON",
                    "head": text[:300]}, "not_json"
    try:
        return json.loads(m.group(0)), detail, "ok"
    except ValueError as e:
        return {}, {**detail, "reason": f"unparseable JSON from the judge: {e}",
                    "head": text[:300]}, "not_json"


def show(card: dict) -> None:
    """The judge's marking, point by point, with its own words next to it —
    because the judging is what a person has to be able to check."""
    mark = {"hit": f"{GRN}hit    {Z}", "partial": f"{YEL}partial{Z}", "miss": f"{RED}miss   {Z}"}
    for p in card.get("points") or []:
        v = str(p.get("verdict", "miss")).lower()
        info(f"  {mark.get(v, v)}  {str(p.get('point', ''))[:88]}")
        if p.get("evidence"):
            info(f"           └ {str(p['evidence'])[:150]}")
    for key, label in (("unsupported_claim", "a claim resting on no record"),
                       ("missed_fact", "a recorded fact that was missed")):
        d = card.get(key) or {}
        if d.get("found"):
            info(f"  {RED}{label}{Z}: {str(d.get('quote', ''))[:160]}")
            if d.get("why"):
                info(f"           └ {str(d['why'])[:150]}")
    if card.get("note"):
        info(f"  judge: {str(card['note'])[:200]}")


def main() -> int:
    banner("STAGE 3 · a model marks the answers against key points written in advance")
    t = Tally()
    src = ROOT / "stage3-input.json"
    if not src.exists():
        t.record(BLOCKED, "there are answers to mark",
                 f"{src} does not exist — stage 2 did not run, or it could not produce answers.")
        write_verdict("3", t.verdict)
        return 0
    doc = json.loads(src.read_text(encoding="utf-8"))
    answers = [a for a in doc.get("answers") or [] if (a.get("answer") or "").strip()]
    if not answers:
        t.record(BLOCKED, "there are answers to mark",
                 "stage 2 produced no non-empty answer, so there is nothing to judge. "
                 "This is reported rather than passed: an unmarked release is not a marked one.")
        write_verdict("3", t.verdict)
        return 0

    ok, why = SM.available()
    if not ok:
        t.record(BLOCKED, "a model is reachable to do the marking",
                 f"{why}\nStage 3 cannot run. It is NOT a pass: whether the answers are right "
                 f"is unknown, and the release decision has to be made knowing that.")
        write_verdict("3", t.verdict)
        return 0
    info(f"judge: {why}")
    info(f"marking {len(answers)} answer(s) · the judge is a model, so read the verdicts as "
         f"evidence, not proof")

    cards = []
    for a in answers:
        step(f"{a['id']} · {a['surface']} · {a['question'][:70]}")
        card, detail, outcome = judge_one(a)
        if outcome != "ok":
            t.record(BLOCKED, f"{a['id']}: marked",
                     f"the judge did not answer ({outcome}): "
                     f"{detail.get('reason') or detail.get('result') or ''}")
            cards.append({"id": a["id"], "outcome": outcome, "detail": detail})
            continue
        show(card)
        pts = card.get("points") or []
        hits = sum(1 for p in pts if str(p.get("verdict", "")).lower() == "hit")
        misses = [p for p in pts if str(p.get("verdict", "")).lower() == "miss"]
        partial = [p for p in pts if str(p.get("verdict", "")).lower() == "partial"]
        bad_claim = bool((card.get("unsupported_claim") or {}).get("found"))
        missed = bool((card.get("missed_fact") or {}).get("found"))
        summary = (f"{hits}/{len(pts)} key points hit"
                   + (f", {len(partial)} partial" if partial else "")
                   + (f", {len(misses)} missed" if misses else "")
                   + (", a claim resting on no record" if bad_claim else "")
                   + (", a recorded fact missed" if missed else ""))
        # An unsupported claim fails on its own: it is the one output of this
        # tool that is worse than no output, because it travels as evidence.
        verdict = FAIL if (bad_claim or misses) else OK
        t.record(verdict, f"{a['id']}: {summary}",
                 "" if verdict == OK else "the judge is a model — read its words above and "
                                          "decide; a disagreement here wants a person, not an "
                                          "automatic pass or fail")
        cards.append({"id": a["id"], "outcome": "ok", "card": card})

    out = ROOT / "stage3-marking.json"
    out.write_text(json.dumps(cards, indent=1, default=str), encoding="utf-8")
    info(f"the full marking, with the judge's own words: {out}")
    print()
    info("The judge is a model. Its verdicts are evidence, not proof: it can misread a right")
    info("answer and it can wave through a wrong one. Where it disagrees with the answer, that")
    info("is a place for a person to look — not a result to accept either way.")
    write_verdict("3", t.verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
