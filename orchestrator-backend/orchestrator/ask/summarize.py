"""ask.summarize — the only thing the model writes, and what code removes from
it (DP phase 2e, Task 2; spec §21, J1 / J2 / J7).

The model is given the question, the fact table, the absence sentences and the
scope line, and is asked for one paragraph in which every sentence ends with
the id of the fact it rests on. Then code goes through it sentence by sentence:

  no id at all              → deleted (J1, veto)
  an id the table lacks     → deleted
  a number the table does
  not state                 → the whole sentence is deleted (J2, veto)
  more than MAX_SENTENCES   → the tail is deleted

Every deletion is counted and the count travels with the answer, to the reader
and into `ask_log` — a trimmed answer never looks like a complete one. With no
runner at all there is no summary and the fact table itself is the answer (J7);
the reader is told so in one line, never by an empty page.
"""
from __future__ import annotations

import re
from importlib import resources

from . import facts as F

MAX_SENTENCES = 8
NO_MODEL_NOTE = "summary unavailable: no model"
DEFAULT_TIMEOUT_S = 120.0
_SPLIT = re.compile(r"(?<=[.!?\]])\s+(?!\[)")
_CITE_TOKEN = re.compile(r"\[#[A-Za-z]{0,2}\d+\]|\[scope\]")


def prompt_text() -> str:
    return resources.files("orchestrator.testing").joinpath("prompts/ask.md").read_text(encoding="utf-8")


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


def check(sentence: str, *, ids, numbers) -> tuple[bool, str | None, list[str]]:
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


def summarize(question: str, ft: dict, *, absences=(), scope_line: str = "", runner=None, model: str | None = None,
              timeout_s: float | None = None, max_sentences: int = MAX_SENTENCES) -> dict:
    """The checked answer. Without a runner: degraded, and the fact table stands."""
    facts_text = F.render(ft)
    absences = list(absences or ())
    allowed_numbers = set(ft.get("numbers") or F.numbers_in(facts_text))
    allowed_numbers |= F.numbers_in(scope_line)
    for a in absences:
        allowed_numbers |= F.numbers_in(a["text"])
    base = {"facts_text": facts_text, "scope_line": scope_line, "model": model,
            "dropped": {"uncited": 0, "unknown_id": 0, "number": 0, "over_limit": 0}, "dropped_detail": []}
    if runner is None:
        return {**base, "answer": "", "sentences": [], "cites": [], "degraded": True, "note": NO_MODEL_NOTE, "raw": None}

    kwargs = {"model": model}
    if timeout_s is not None:
        kwargs["timeout_s"] = timeout_s
    try:
        raw = runner(prompt_for(question, facts_text, absences, scope_line), **kwargs)
    except Exception:
        return {**base, "answer": "", "sentences": [], "cites": [], "degraded": True,
                "note": NO_MODEL_NOTE, "raw": None}
    if not (raw or "").strip():
        return {**base, "answer": "", "sentences": [], "cites": [], "degraded": True,
                "note": NO_MODEL_NOTE, "raw": raw}

    kept: list[str] = []
    dropped = dict(base["dropped"])
    detail: list[dict] = []
    for s in split_sentences(raw):
        if len(kept) >= max_sentences:
            dropped["over_limit"] += 1
            detail.append({"text": s, "reason": "over_limit", "numbers": []})
            continue
        ok, reason, bad = check(s, ids=ft["ids"], numbers=allowed_numbers)
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
    note = None
    if sum(dropped.values()):
        note = ", ".join(f"{n} {label}" for label, n in
                         (("uncited", dropped["uncited"]), ("unknown id", dropped["unknown_id"]),
                          ("number not in the fact table", dropped["number"]), ("over the limit", dropped["over_limit"]))
                         if n)
        note = f"{sum(dropped.values())} sentence(s) dropped: {note}"
    return {**base, "answer": " ".join(kept), "sentences": kept, "cites": cites, "dropped": dropped,
            "dropped_detail": detail, "degraded": False, "note": note, "raw": raw}
