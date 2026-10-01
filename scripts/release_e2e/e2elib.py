"""e2elib — the verdicts, the printing and the sandbox paths, for the Python stages.

The same four verdicts as common.sh, and the same rule: BLOCKED is not OK. A
check that cannot run says so; it never returns a quiet pass.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

OK, FAIL, FINDING, BLOCKED = 0, 1, 2, 3
_RANK = {OK: 0, FINDING: 1, BLOCKED: 2, FAIL: 3}
_NAME = {OK: "OK", FAIL: "FAIL", FINDING: "FINDING", BLOCKED: "BLOCKED"}


def worst(*verdicts: int) -> int:
    return max(verdicts, key=lambda v: _RANK.get(v, 3)) if verdicts else OK


def name(verdict: int) -> str:
    return _NAME.get(verdict, "FAIL")


_TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
RED, GRN, YEL, BLU, B, Z = (
    ("\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[1m", "\033[0m") if _TTY else ("",) * 6
)

ROOT = Path(os.environ.get("E2E_ROOT", "/tmp"))
LOGS = Path(os.environ.get("E2E_LOGS", ROOT / "logs"))
FINDINGS = Path(os.environ.get("E2E_FINDINGS", ROOT / "findings.txt"))
UNDER_TEST = Path(os.environ.get("E2E_UNDER_TEST", Path(__file__).resolve().parents[2]))


def banner(text: str) -> None:
    print(f"\n{B}{BLU}{text}{Z}\n" + "─" * 72)


def step(text: str) -> None:
    print(f"{BLU}·{Z} {text}")


def info(text: str) -> None:
    print(f"    {text}")


class Tally:
    """Every check's verdict, printed as it happens and folded at the end."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, str]] = []
        self._said: set[str] = set()

    def record(self, verdict: int, label: str, detail: str = "") -> int:
        mark = {OK: f"  {GRN}PASS{Z}   ", FAIL: f"  {RED}FAIL{Z}   ",
                FINDING: f"  {GRN}PASS{Z}*  ", BLOCKED: f"  {YEL}BLOCKED{Z}"}[verdict]
        print(f"{mark} {label}")
        if detail:
            for line in str(detail).splitlines():
                print(f"           {line}")
        self.rows.append((verdict, label))
        return verdict

    def finding(self, what: str, deviation: str) -> int:
        """A place where the documents and reality disagree. Loud, and kept —
        but said once per run however many callers walk into it."""
        if what in self._said:
            return FINDING
        self._said.add(what)
        print(f"  {B}{YEL}FINDING{Z} {what}")
        print(f"            deviation: {deviation}")
        try:
            with FINDINGS.open("a", encoding="utf-8") as f:
                f.write(f"FINDING · {what} :: deviation: {deviation}\n")
        except OSError:
            pass
        self.rows.append((FINDING, what))
        return FINDING

    @property
    def verdict(self) -> int:
        return worst(*(v for v, _ in self.rows)) if self.rows else OK


def write_verdict(stage: str, verdict: int) -> None:
    print(f"\n  stage {stage} verdict: {B}{name(verdict)}{Z}")
    (ROOT / f"stage{stage}.verdict").write_text(str(verdict), encoding="utf-8")
