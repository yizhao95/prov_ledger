"""ask.runner — the model call, and the reason it did not answer.

A runner is any callable ``f(prompt, *, model=None, timeout_s=None)``. It may
return the text alone (the shape every stub in the suite uses) or
``(text, detail)`` — a dict of whatever the call actually knows: the return
code, the head of stderr, how long it took, how long the answer was. A runner
that cannot run at all raises; `RunnerError` carries the same `detail`, and
`RunnerTimeout` says the budget was spent rather than that the model was silent.

`call()` is the ONLY place that decides which of those happened, and it names
the outcome:

    ok       the model answered
    empty    the model was reached and said nothing (rc / stderr say why)
    refused  the model answered ABOUT ITSELF — a quota, a login, a policy — and
             that sentence is the most useful thing in the whole run
    failed   the call raised — the exception is kept verbatim
    timeout  the budget ran out

Before this module they collapsed into one sentence, "summary unavailable: no
model", and a packaging bug (`prompt_text` asking for a module name the wheel
does not install) read to the person at the terminal as a missing model. The
detail this returns is written to `ask_log.runner_detail`, append-only, so the
next person does not have to guess either.

`PROVLEDGER_ASK_MODEL` names the model, and `--model` beats it. Nothing here
ever retries with a different one: an answer whose model was swapped in
silently is an answer whose provenance is a guess. The note says which model
declined and suggests one, and a person decides.
"""
from __future__ import annotations

import os
import time

# The budget a person waits through for one headless model call. `ask.BUDGET_S`
# is the budget for the CODE; this one is for the model, which is slower by
# nature and must still be bounded.
DEFAULT_TIMEOUT_S = 180.0
STDERR_HEAD = 400

ASK_MODEL_ENV = "PROVLEDGER_ASK_MODEL"
OUTCOMES = ("ok", "empty", "refused", "failed", "timeout")
# outcomes of the ANSWER rather than of the call, decided by the caller
ANSWER_OUTCOMES = ("not_json", "no_model", "no_candidates")


def model_for(model: str | None = None) -> str | None:
    """The model this call runs on: the argument, else $PROVLEDGER_ASK_MODEL."""
    return model or ((os.environ.get(ASK_MODEL_ENV) or "").strip() or None)


class RunnerError(Exception):
    """A model call that did not produce text. `detail` is what to write down."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = dict(detail or {})


class RunnerTimeout(RunnerError):
    """The budget ran out. Not the same thing as an empty answer."""


def normalise(result) -> tuple[str, dict]:
    """A runner may return `text` or `(text, detail)`; here both are (text, detail)."""
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], (dict, type(None))):
        return (result[0] or ""), dict(result[1] or {})
    return (result or ""), {}


def head(text: str | None, limit: int = STDERR_HEAD) -> str:
    """The first `limit` characters of a stream, on one line — a pointer, not a body."""
    s = " ".join((text or "").split())
    return s[:limit]


def call(runner, prompt: str, *, model: str | None = None, timeout_s: float | None = None) -> tuple[str, dict, str]:
    """(text, detail, outcome) for one model call. Never raises for the caller."""
    model = model_for(model)
    kwargs = {"model": model}
    if timeout_s is not None:
        kwargs["timeout_s"] = timeout_s
    started = time.perf_counter()

    def ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        text, detail = normalise(runner(prompt, **kwargs))
    except RunnerTimeout as e:
        detail = {"timeout_s": timeout_s, **e.detail}
        detail.setdefault("elapsed_ms", ms())
        detail.setdefault("error", f"{type(e).__name__}: {e}")
        return "", detail, "timeout"
    except Exception as e:                                   # a runner that dies says which death it died
        detail = dict(getattr(e, "detail", None) or {})
        detail.setdefault("elapsed_ms", ms())
        detail["error"] = f"{type(e).__name__}: {e}"
        return "", detail, "failed"
    detail.setdefault("elapsed_ms", ms())
    detail.setdefault("prompt_chars", len(prompt or ""))
    detail.setdefault("model", model)
    detail["raw_len"] = len(text)
    if text.strip():
        return text, detail, "ok"
    # the runner heard the model talk about itself rather than about the question
    return text, detail, ("refused" if detail.get("refused") else "empty")


def why_empty(detail: dict) -> str:
    """The parenthetical after "model returned nothing": rc, the head of stderr."""
    bits = []
    if detail.get("rc") is not None:
        bits.append(f"rc {detail['rc']}")
    if detail.get("reason"):
        bits.append(str(detail["reason"]))
    if detail.get("stderr_head"):
        bits.append(f"stderr: {detail['stderr_head']}")
    return "; ".join(bits)
