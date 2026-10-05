"""Tests for docs/cli.md — the one reference table of `provledger` commands.

Every subcommand printed in the table must really be registered in
`orchestrator/cli.py`, so the reference cannot drift away from the tool it
documents. (This check used to live in test_readme_v2.py, back when the table
was part of the README.)
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "cli.md"
CLI = REPO / "orchestrator-backend" / "orchestrator" / "cli.py"


def cli_table_commands(doc: str) -> list[str]:
    """The backticked first cell of every row in the Commands table."""
    section = doc.split("\n## Commands\n", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("| `")]
    assert rows, "no rows found in the Commands table"
    out = []
    for row in rows:
        cell = row.split("|")[1]
        for name in re.findall(r"`([^`]+)`", cell):
            # `node declare` / `metrics plan` / `init --agents-md`
            words = [w for w in name.split() if not w.startswith("-")]
            if words:
                out.append(" ".join(words))
    return out


def registered_subcommands() -> set[str]:
    """Every word the CLI dispatches on.

    Most are argparse subparsers. `anchor check` / `anchor candidates` are a
    positional the parser compares by value (`args.target == "check"`), so
    those literals count too.
    """
    src = CLI.read_text(encoding="utf-8")
    names = set(re.findall(r'add_parser\(\s*"([^"]+)"', src))
    names |= set(re.findall(r'args\.target\s*==\s*"([^"]+)"', src))
    return names


def test_the_table_is_not_empty() -> None:
    assert len(cli_table_commands(DOC.read_text(encoding="utf-8"))) >= 15


def test_every_command_in_the_table_exists() -> None:
    registered = registered_subcommands()
    missing = sorted(
        {cmd for cmd in cli_table_commands(DOC.read_text(encoding="utf-8"))
         if not all(word in registered for word in cmd.split())}
    )
    assert not missing, f"docs/cli.md documents commands cli.py does not register: {missing}"
