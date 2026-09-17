"""ClaudeArbiter — a headless-`claude` arbiter (phase 8 Task 2, FL-040).

One `claude -p --output-format json --max-turns 1 --tools ""` call per
ambiguity, the prompt on stdin (prompts/arbiter.md + the ambiguity as JSON: both
sides' qualified names, files, line numbers, ±10 context lines and the
signature equalities). No tools, no session, no API key — the user's own
Claude Code login. The answer must be one strict JSON object
`{"pairs": [[prev_qn, cur_qn], ...], "evidence": "..."}`; anything else
(non-JSON, empty evidence, a name outside the ambiguity, a name used twice)
means NO assertion for that ambiguity. Like every arbiter it is wired into
the analyzer only after `provledger.testing.calibration.gate` passes, and
it never runs in CI: tests inject a stub `runner`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from importlib import resources
from pathlib import Path

from ..ask import runner as R
from ..graph_api import Ambiguity, Assertion, Row

ARBITER_ID = "anthropic.claude_headless"
DEFAULT_TIMEOUT_S = 120.0
CONTEXT_LINES = 10
_JSON_RE = re.compile(r"\{.*\}", re.S)


def prompt_text() -> str:
    return resources.files(__package__).joinpath("prompts/arbiter.md").read_text(encoding="utf-8")


def claude_command(model: str | None = None) -> list[str]:
    cmd = ["claude", "-p", "--output-format", "json", "--max-turns", "1", "--tools", "", "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    return cmd


def default_runner(prompt: str, *, model: str | None = None,
                   timeout_s: float = DEFAULT_TIMEOUT_S) -> tuple[str, dict]:
    """Run headless claude with `prompt` on stdin from a neutral cwd and return
    `(result text, detail)`.

    It used to return `''` for a timeout, a non-zero exit and unparseable output
    alike, and the caller printed "no model" for all three. Now the reason
    travels: `detail` carries the return code, the head of stderr, the wall time
    and the command, a timeout raises `RunnerTimeout`, and a `claude` that is
    not on PATH raises `RunnerError`. Whoever called decides what to say —
    `text_runner` is the thin wrapper for callers that only want the text."""
    import time

    cmd = claude_command(model)
    detail = {"cmd": " ".join(cmd), "model": model, "timeout_s": timeout_s, "prompt_chars": len(prompt or "")}
    started = time.perf_counter()

    def timed() -> dict:
        return {**detail, "elapsed_ms": int((time.perf_counter() - started) * 1000)}

    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout_s, cwd=os.environ.get("TMPDIR") or "/tmp")
    except subprocess.TimeoutExpired as e:
        raise R.RunnerTimeout(f"claude did not answer within {timeout_s} s",
                              {**timed(), "stderr_head": R.head(getattr(e, "stderr", None))}) from e
    except OSError as e:
        raise R.RunnerError(f"could not run claude: {e}", {**timed(), "error": f"{type(e).__name__}: {e}"}) from e
    d = {**timed(), "rc": p.returncode, "stderr_head": R.head(p.stderr), "stdout_chars": len(p.stdout or "")}
    if p.returncode != 0:
        return "", {**d, "reason": "non-zero exit"}
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        return "", {**d, "reason": "output was not JSON", "stdout_head": R.head(p.stdout)}
    if not isinstance(doc, dict):
        return "", {**d, "reason": "output was not a JSON object"}
    text = doc.get("result") or ""
    if not text:
        d["reason"] = "the JSON answer had no `result`"
    return text, d


def text_runner(prompt: str, *, model: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    """`default_runner` for callers that want the text and nothing else — the
    arbiter, which treats every silence the same way (no assertion)."""
    try:
        return default_runner(prompt, model=model, timeout_s=timeout_s)[0]
    except R.RunnerError:
        return ""


def parse_answer(text: str | None) -> tuple[list[tuple[str, str]], str] | None:
    """The strict answer shape, or None. A code fence around the object is tolerated."""
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        doc = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("pairs"), list) or not isinstance(doc.get("evidence"), str):
        return None
    pairs: list[tuple[str, str]] = []
    for p in doc["pairs"]:
        if not (isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) and x for x in p)):
            return None
        pairs.append((p[0], p[1]))
    return pairs, doc["evidence"]


def _context_from_repo(repo: str | None, row: Row) -> str | None:
    if not repo or not row.file_path or not row.line_start:
        return None
    p = Path(repo) / row.file_path
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    lo = max(1, int(row.line_start) - CONTEXT_LINES)
    hi = min(len(lines), int(row.line_end or row.line_start) + CONTEXT_LINES)
    return "\n".join(f"{n:5d}  {lines[n - 1]}" for n in range(lo, hi + 1))


class ClaudeArbiter:
    arbiter_id = ARBITER_ID

    def __init__(self, *, model: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S, runner=None, repo: str | None = None):
        self.model = model or os.environ.get("PROVLEDGER_ARBITER_MODEL") or None
        self.timeout_s = timeout_s
        self.runner = runner or text_runner
        self.repo = repo or os.environ.get("PROVLEDGER_ARBITER_REPO") or None
        self.exchanges: list[dict] = []      # (prompt, raw answer, verdict) per call — for the dogfood log

    def bind_repo(self, repo: str | None) -> None:
        """The analyzer tells the arbiter where the working tree is (context lines for live rows)."""
        if repo and not self.repo:
            self.repo = repo

    def _row(self, r: Row) -> dict:
        return {"qualified_name": r.qualified_name, "node_type": r.node_type, "file_path": r.file_path,
                "lines": [r.line_start, r.line_end],
                "context": r.context if r.context is not None else _context_from_repo(self.repo, r)}

    def payload(self, amb: Ambiguity) -> dict:
        rows = amb.prev + amb.cur
        return {"layer": amb.layer,
                "signatures_equal": {"qualified_name": False,
                                     "struct_sig": len({r.struct_sig for r in rows}) == 1,
                                     "dataflow_sig": len({r.dataflow_sig for r in rows}) == 1},
                "previous": [self._row(r) for r in amb.prev],
                "current": [self._row(r) for r in amb.cur]}

    def prompt_for(self, amb: Ambiguity) -> str:
        return prompt_text().rstrip() + "\n\nMaterial:\n" + json.dumps(self.payload(amb), indent=1, sort_keys=True) + "\n"

    def arbitrate(self, ambiguities: list[Ambiguity]) -> list[Assertion]:
        out: list[Assertion] = []
        for amb in ambiguities:
            prompt = self.prompt_for(amb)
            raw = self.runner(prompt, model=self.model, timeout_s=self.timeout_s)
            verdict = self._judge(amb, raw)
            self.exchanges.append({"prompt": prompt, "raw": raw, "verdict": verdict[1]})
            out.extend(verdict[0])
        return out

    def _judge(self, amb: Ambiguity, raw: str | None) -> tuple[list[Assertion], str]:
        parsed = parse_answer(raw)
        if parsed is None:
            return [], "rejected: not the JSON answer shape"
        pairs, evidence = parsed
        if not evidence.strip():
            return [], "rejected: empty evidence"
        prev_by_qn = {r.qualified_name: r for r in amb.prev}
        cur_qns = {r.qualified_name for r in amb.cur}
        if any(p not in prev_by_qn or c not in cur_qns for p, c in pairs):
            return [], "rejected: a name outside the ambiguity"
        if len({p for p, _ in pairs}) != len(pairs) or len({c for _, c in pairs}) != len(pairs):
            return [], "rejected: a name used twice"
        if not pairs:
            return [], "abstained"
        return [Assertion(cur_qualified_name=c, chosen_prev_key=prev_by_qn[p].node_key, evidence=evidence.strip(),
                          arbiter=self.arbiter_id) for p, c in pairs], "asserted"
