"""session_model — the headless `claude` call, used twice and for two different
jobs, which must not be confused.

**As the stand-in session model (stage 2).** `/ledger` and `/receipts` are
instructions for the model that is already in the room: the CLI fetches rows and
computes absences, and the session's own model picks, writes and cites. A
release check has no session, so it has to play that part itself — one headless
call per question, handed the skill's own SKILL.md and the material the CLI
computed. This is a stand-in, said out loud: the real thing has the session's
context and its tools, and this does not.

**As the judge (stage 3).** A second, separate call that sees the question, the
answer and the key points, and says which points the answer actually hit.

Both borrow the isolated-settings trick from
`orchestrator/testing/claude_arbiter.py`: `claude -p` otherwise reads the
host's `~/.claude/settings.json`, and on the machine that was found on it said
`"language": "Chinese"`, so every headless call answered in Chinese against
prompts written in English. A settings file of our own fixes the language and
disables the host's plugins, whose paragraphs otherwise land in front of the
answer.

Nothing here ever retries with a different model. An answer whose model was
swapped in silently is an answer whose provenance is a guess.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ISOLATED_SETTINGS = {"language": "en", "enabledPlugins": {}}
DEFAULT_TIMEOUT_S = 240.0
_SETTINGS: str | None = None


def settings_path() -> str:
    """The settings file every call here runs with: ours, not the host's."""
    global _SETTINGS
    override = (os.environ.get("PROVLEDGER_CLAUDE_SETTINGS") or "").strip()
    if override and os.path.exists(override):
        return override
    if _SETTINGS and os.path.exists(_SETTINGS):
        return _SETTINGS
    fd, path = tempfile.mkstemp(prefix="release-e2e-claude-", suffix=".settings.json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(ISOLATED_SETTINGS, f)
    _SETTINGS = path
    return path


def command(model: str | None = None) -> list[str]:
    cmd = ["claude", "-p", "--output-format", "json", "--max-turns", "1",
           "--tools", "", "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    return cmd + ["--settings", settings_path()]


def available() -> tuple[bool, str]:
    """Is there a model to call at all? Answered by calling it, not by looking
    for the binary: a `claude` on PATH that is not logged in, or out of quota,
    is exactly as unusable and must not read as available."""
    text, detail, outcome = call("Reply with exactly: PONG", timeout_s=90)
    if outcome == "ok" and "PONG" in text.upper():
        return True, f"claude answered in {detail.get('elapsed_ms', '?')} ms"
    said = detail.get("result") or detail.get("reason") or detail.get("error") or ""
    return False, f"{outcome}: {str(said)[:300] or 'no answer'}"


def call(prompt: str, *, model: str | None = None,
         timeout_s: float = DEFAULT_TIMEOUT_S) -> tuple[str, dict, str]:
    """(text, detail, outcome) for one call. Outcomes, as ask.runner names them:
    ok · empty · refused (the model talked about itself) · failed · timeout ·
    blocked (not made: the login would expire during it; see plugin_session.login_ready)."""
    cmd = command(model)
    detail: dict = {"cmd": " ".join(cmd[:8]) + " …", "model": model,
                    "prompt_chars": len(prompt or ""), "timeout_s": timeout_s}
    import time
    started = time.perf_counter()

    def timed(d: dict) -> dict:
        return {**d, "elapsed_ms": int((time.perf_counter() - started) * 1000)}

    if os.environ.get("CLAUDE_CONFIG_DIR"):      # the sandbox's login, current (see plugin_session.login_ready)
        import plugin_session
        ready, why = plugin_session.login_ready(os.environ["CLAUDE_CONFIG_DIR"],
                                                os.environ.get("E2E_REAL_HOME") or Path.home(), timeout_s)
        if not ready:
            return "", timed({**detail, "reason": why}), "blocked"
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout_s, cwd=os.environ.get("TMPDIR") or "/tmp")
    except subprocess.TimeoutExpired:
        return "", timed({**detail, "reason": f"no answer within {timeout_s:g}s"}), "timeout"
    except OSError as e:
        return "", timed({**detail, "error": f"{type(e).__name__}: {e}"}), "failed"
    detail = timed({**detail, "rc": p.returncode,
                    "stderr_head": " ".join((p.stderr or "").split())[:300]})
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        return "", {**detail, "reason": "output was not JSON",
                    "stdout_head": (p.stdout or "")[:300]}, "failed"
    said = doc["result"] if isinstance(doc.get("result"), str) else ""
    # Read the answer BEFORE judging the exit code: a model that declines — a
    # quota, a login — exits non-zero and prints that sentence as good JSON, and
    # that sentence is the most useful thing in the whole run.
    if doc.get("is_error") or (said and p.returncode != 0):
        return "", {**detail, "refused": True, "result": said,
                    "reason": "the model answered about itself, not about the question"}, "refused"
    if p.returncode != 0:
        return "", {**detail, "reason": "non-zero exit"}, "failed"
    return (said, detail, "ok") if said.strip() else ("", {**detail, "reason": "no result"}, "empty")


def skill(under_test: Path, name: str) -> str:
    """A bundled skill's own instructions — the ones that actually ship."""
    p = Path(under_test) / "skills" / name / "SKILL.md"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""
