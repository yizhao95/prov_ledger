"""ask.summarize — the only thing the model writes, and what code removes from
it (DP phase 2e, Task 2; spec §21, J1 / J2 / J7).

The model is given the question, the fact table, the absence sentences and the
scope line, and must answer with ONE JSON object, `{"sentences": [...]}`, each
sentence ending in the id of the fact it rests on. The JSON is not decoration:
the host's plugins write into `claude -p`'s `result` (claude-mem prepends
"Memory capture is currently paused…" to every answer on this machine), and
read as prose that paragraph became "a sentence the model made up with no
citation". Taking an object out of the reply drops the noise by construction.

Then code goes through the sentences one by one:

  no id at all              → deleted (J1, veto)
  an id the table lacks     → deleted
  a number the table does
  not state                 → the whole sentence is deleted (J2, veto)
  more than MAX_SENTENCES   → the tail is deleted

Language is NOT one of them. It used to be, and deleting for it deleted a
correct answer — `(nothing survived the checks)` over a paragraph whose every
citation was right, because the host's settings had the model answering in
Chinese. Citations and numbers are correctness; language is a preference, so a
mismatch is reported (`language_mismatch`) and the sentences are kept.

Every deletion is counted and the count travels with the answer, to the reader
and into `ask_log` — a trimmed answer never looks like a complete one. With no
runner at all there is no summary and the fact table itself is the answer (J7);
the reader is told so in one line, never by an empty page.
"""
from __future__ import annotations

import json
import re
from importlib import resources

from . import facts as F, runner as R

MAX_SENTENCES = 8
# One note per cause. They used to be one sentence, "summary unavailable: no
# model", printed for four different things — including a packaging bug (see
# TESTING_PACKAGE below), which read as "you have no model".
NO_MODEL_NOTE = "summary unavailable: no model configured"
NO_CANDIDATES_NOTE = "summary unavailable: no candidate nodes matched the question"
DEFAULT_TIMEOUT_S = R.DEFAULT_TIMEOUT_S
# FL-067: the wheel installs this backend as `provledger`, the repo imports it
# as `orchestrator`. The prompt lives one package up from this one, whatever
# that package is called — a literal name here is a ModuleNotFoundError in
# every installed copy, and that is exactly what happened.
TESTING_PACKAGE = __package__.rsplit(".", 1)[0] + ".testing"
DROP_KINDS = ("uncited", "unknown_id", "number", "over_limit")
# The answer the model must produce. Anything the host's plugins prepend to
# `result` sits outside this object and is discarded with it.
ANSWER_KEY = "sentences"
_JSON_RE = re.compile(r"\{.*\}", re.S)
# The language the reader asked in. A mismatch is said, never deleted: the
# machine cannot tell a wrong answer from one in the wrong language, and it was
# deleting the right answer. `--lang` switches what is asked for; the model's
# own language comes from the settings file the call runs with
# (`claude_arbiter.settings_path`), not from the prompt.
LANG_NAMES = {"en": "English", "zh": "Chinese"}
LANG_HINT = 'ask with `--lang {other}`, or check "language" in ~/.claude/settings.json'
_OTHER_LANG = {"en": "zh", "zh": "en"}
# A refusal is a pointer, not a body: enough of the model's own sentence to act
# on. No model is ever substituted for another — an answer whose model was
# swapped in silently is an answer whose provenance is a guess — so the note
# names one and a person decides.
REFUSAL_HEAD = 200
REFUSAL_HINT = "try --model sonnet (no model is substituted automatically)"
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_SPLIT = re.compile(r"(?<=[.!?\]])\s+(?!\[)")
_CITE_TOKEN = re.compile(r"\[#[A-Za-z]{0,2}\d+\]|\[scope\]")


def prompt_text() -> str:
    return resources.files(TESTING_PACKAGE).joinpath("prompts/ask.md").read_text(encoding="utf-8")


def prompt_for(question: str, facts_text: str, absences=(), scope_line: str = "") -> str:
    lines = [a["text"] for a in absences] or ["(none)"]
    return (prompt_text()
            .replace("{question}", question.strip())
            .replace("{facts}", facts_text)
            .replace("{absences}", "\n".join(lines))
            .replace("{scope}", scope_line or "(not computed)"))


def split_sentences(text: str) -> list[str]:
    """Sentences, keeping a trailing `[#id]` / `[scope]` attached to its own sentence."""
    out = []
    for chunk in (text or "").replace("\r", "").split("\n"):
        for s in _SPLIT.split(chunk):
            s = s.strip()
            if s:
                out.append(s)
    return out


def no_drops() -> dict:
    return dict.fromkeys(DROP_KINDS, 0)


def parse_sentences(raw: str | None) -> list[str] | None:
    """The sentences out of the model's JSON object, or None if it did not send
    one. A code fence, and any preamble a host plugin injected, fall off here."""
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        doc = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get(ANSWER_KEY), list):
        return None
    out = [x.strip() for x in doc[ANSWER_KEY] if isinstance(x, str) and x.strip()]
    return out


def language_of(text: str, lang: str) -> bool:
    """True when `text` is in the language that was asked for (CJK vs not)."""
    return bool(_CJK.search(text)) if lang == "zh" else not _CJK.search(text)


def language_mismatch(sentences, lang: str) -> str | None:
    """None | 'some' | 'all' — how much of the answer is in another language."""
    wrong = [s for s in sentences if not language_of(s, lang)]
    if not wrong:
        return None
    return "all" if len(wrong) == len(sentences) else "some"


def language_note(mismatch: str | None, lang: str) -> str | None:
    if not mismatch:
        return None
    hint = LANG_HINT.format(other=_OTHER_LANG.get(lang, "zh"))
    want = LANG_NAMES.get(lang, lang)
    if mismatch == "all":
        return (f"the whole answer came back in another language, not {want} — it was kept, and every "
                f"citation in it was still checked; {hint}")
    return f"part of the answer is not in {want} — the sentences were kept; {hint}"


def check(sentence: str, *, ids, numbers, lang: str = "en") -> tuple[bool, str | None, list[str]]:
    """(keep, reason, offending numbers) for one sentence."""
    cites = F.cites_in(sentence)
    has_scope = "[scope]" in sentence
    if not cites and not has_scope:
        return False, "uncited", []
    unknown = sorted(c for c in cites if c not in ids)
    if unknown:
        return False, "unknown_id", []
    bare = _CITE_TOKEN.sub(" ", sentence)
    bad = sorted(F.numbers_in(bare) - set(numbers))
    if bad:
        return False, "number", bad
    return True, None, []


def allowed_numbers(ft: dict, absences=(), scope_line: str = "") -> set[str]:
    """Every number an answer may contain: the fact table's, plus the ones the
    code itself printed in the scope line and the absence sentences."""
    nums = set(ft.get("numbers") or F.numbers_in(F.render(ft)))
    nums |= F.numbers_in(scope_line)
    for a in absences or ():
        nums |= F.numbers_in(a["text"])
    return nums


def review(draft: str, ft: dict, *, absences=(), scope_line: str = "",
           max_sentences: int = MAX_SENTENCES, lang: str = "en") -> dict:
    """The checks, and the ONLY implementation of them: whoever wrote the draft —
    a headless model, or the session's own model through `provledger ask submit`
    — it is read back the same way. Returns {answer, sentences, cites, dropped,
    dropped_detail, note}."""
    numbers = allowed_numbers(ft, absences, scope_line)
    kept: list[str] = []
    dropped = no_drops()
    detail: list[dict] = []
    for s in split_sentences(draft):
        if len(kept) >= max_sentences:
            dropped["over_limit"] += 1
            detail.append({"text": s, "reason": "over_limit", "numbers": []})
            continue
        ok, reason, bad = check(s, ids=ft["ids"], numbers=numbers, lang=lang)
        if ok:
            kept.append(s)
        else:
            dropped[reason] += 1
            detail.append({"text": s, "reason": reason, "numbers": bad})
    cites: list[str] = []
    for s in kept:
        for c in sorted(F.cites_in(s)):
            if c not in cites:
                cites.append(c)
    parts = []
    if sum(dropped.values()):
        why = ", ".join(f"{n} {label}" for label, n in
                        (("uncited", dropped["uncited"]), ("unknown id", dropped["unknown_id"]),
                         ("number not in the fact table", dropped["number"]),
                         ("over the limit", dropped["over_limit"]))
                        if n)
        parts.append(f"{sum(dropped.values())} sentence(s) dropped: {why}")
    mismatch = language_mismatch(kept, lang)
    if mismatch:
        parts.append(language_note(mismatch, lang))
    return {"answer": " ".join(kept), "sentences": kept, "cites": cites, "dropped": dropped,
            "dropped_detail": detail, "language_mismatch": mismatch, "note": "; ".join(parts) or None}


def note_for(outcome: str, detail: dict) -> str:
    """The one sentence the reader gets, and it names the cause."""
    if outcome == "timeout":
        return f"summary unavailable: model call timed out after {detail.get('timeout_s')} s"
    if outcome == "failed":
        return f"summary unavailable: model call failed: {detail.get('error') or 'unknown'}"
    if outcome == "not_json":
        raw = detail.get("raw_head")
        return ("summary unavailable: the model did not answer in the JSON shape the prompt requires"
                + (f" (it said: {R.head(raw, REFUSAL_HEAD)})" if raw else ""))
    if outcome == "refused":
        # what the model said about itself is the answer here — a spent quota, a
        # login, a policy. Whoever reads this can act on the sentence; they
        # cannot act on "rc 1".
        who = f" [{detail['model']}]" if detail.get("model") else ""
        said = R.head(detail.get("result"), REFUSAL_HEAD) or "(it said nothing)"
        return f"summary unavailable: model call refused{who}: {said} — {REFUSAL_HINT}"
    why = R.why_empty(detail)
    return "summary unavailable: model returned nothing" + (f" ({why})" if why else "")


def summarize(question: str, ft: dict, *, absences=(), scope_line: str = "", runner=None, model: str | None = None,
              timeout_s: float | None = None, max_sentences: int = MAX_SENTENCES,
              candidates: int | None = None, lang: str = "en") -> dict:
    """The checked answer — or the reason there is none, which is never "no model"
    unless there is genuinely no model. `candidates=0` means the code found
    nothing to ask about, and the model is not called at all."""
    facts_text = F.render(ft)
    absences = list(absences or ())
    base = {"facts_text": facts_text, "scope_line": scope_line, "model": model,
            "dropped": no_drops(), "dropped_detail": []}

    def degraded(reason: str, note: str, detail: dict | None = None, raw: str | None = None) -> dict:
        return {**base, "answer": "", "sentences": [], "cites": [], "degraded": True, "degraded_reason": reason,
                "note": note, "raw": raw, "language_mismatch": None, "runner_detail": dict(detail or {})}

    if candidates == 0:
        return degraded("no_candidates", NO_CANDIDATES_NOTE)
    if runner is None:
        return degraded("no_model", NO_MODEL_NOTE)

    # building the prompt is NOT part of the model call: a prompt that cannot be
    # read is a bug here, and saying "no model" about it wasted a day.
    try:
        prompt = prompt_for(question, facts_text, absences, scope_line)
    except Exception as e:
        return degraded("failed", note_for("failed", {"error": f"{type(e).__name__}: {e}"}),
                        {"error": f"{type(e).__name__}: {e}", "stage": "prompt"})

    raw, detail, outcome = R.call(runner, prompt, model=model, timeout_s=timeout_s)
    if outcome != "ok":
        return degraded(outcome, note_for(outcome, detail), detail, raw or None)

    # the answer is the JSON object inside the reply, never the whole reply: the
    # host's plugins prepend their own paragraphs to `result` and those are not
    # the model's sentences to be judged.
    sentences = parse_sentences(raw)
    if sentences is None:
        d = {**detail, "raw_head": R.head(raw, R.STDERR_HEAD)}
        return degraded("not_json", note_for("not_json", d), d, raw)

    checked = review("\n".join(sentences), ft, absences=absences, scope_line=scope_line,
                     max_sentences=max_sentences, lang=lang)
    return {**base, **checked, "degraded": False, "degraded_reason": None, "raw": raw, "runner_detail": detail}
