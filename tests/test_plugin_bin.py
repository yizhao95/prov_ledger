"""The plugin's bin/provledger and the failed-call hook (FL-195, FL-208).

FL-195: the plugin installs the `provledger` command into its own venv, and
nothing put that venv on the PATH of the Bash tool, so `/ledger` and
`/receipts` — which call `provledger` by name — answered nothing for a plain
plugin user. Claude Code puts a plugin's top-level `bin/` on that PATH while the
plugin is enabled; `bin/provledger` hands every call to the venv's command.

FL-208: a tool call that fails fires PostToolUseFailure, never PostToolUse, so
failed calls were never logged.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "provledger"


def _venv(tmp_path: Path, *, console: bool = True, python: bool = True) -> Path:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    if console:
        stub = venv / "bin" / "provledger"
        stub.write_text('#!/usr/bin/env bash\nprintf "console:"; printf "[%s]" "$@"; echo; exit 3\n')
        stub.chmod(0o755)
    if python:
        stub = venv / "bin" / "python"
        stub.write_text('#!/usr/bin/env bash\nprintf "python:"; printf "[%s]" "$@"; echo; exit 4\n')
        stub.chmod(0o755)
    return venv


def _run(venv: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PROVLEDGER_VENV": str(venv)}
    return subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, env=env, timeout=30)


def test_the_wrapper_is_an_executable_in_the_plugin_bin() -> None:
    assert WRAPPER.is_file() and os.access(WRAPPER, os.X_OK)


def test_every_argument_reaches_the_venv_command_unchanged(tmp_path: Path) -> None:
    proc = _run(_venv(tmp_path), "ask", "why is it 70/30?", "--json")
    assert proc.stdout.strip() == "console:[ask][why is it 70/30?][--json]"
    assert proc.returncode == 3, "the command's own exit code comes back"


def test_without_the_console_script_it_runs_the_module(tmp_path: Path) -> None:
    proc = _run(_venv(tmp_path, console=False), "why", "a.py:3")
    assert proc.stdout.strip() == "python:[-m][provledger.cli][why][a.py:3]"
    assert proc.returncode == 4


def _plugin(tmp_path: Path, bootstrap: str) -> Path:
    """A plugin tree with the real wrapper and a stand-in bootstrap.sh."""
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "bin" / "provledger").write_text(WRAPPER.read_text())
    (root / "bin" / "provledger").chmod(0o755)
    (root / "scripts" / "bootstrap.sh").write_text("#!/usr/bin/env bash\n" + bootstrap)
    return root


def _run_in(root: Path, venv: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PROVLEDGER_VENV": str(venv)}
    return subprocess.run([str(root / "bin" / "provledger"), *args], capture_output=True, text=True, env=env,
                          timeout=30)


def test_without_a_venv_it_runs_the_bootstrap_once_then_the_command(tmp_path: Path) -> None:
    """The session that installed the plugin never ran its SessionStart
    bootstrap; after /reload-plugins its hooks record, and `provledger` must
    work too instead of saying there is no venv."""
    root = _plugin(tmp_path, """echo "bootstrap ran"
mkdir -p "$PROVLEDGER_VENV/bin"
cat > "$PROVLEDGER_VENV/bin/provledger" <<'STUB'
#!/usr/bin/env bash
printf "console:"; printf "[%s]" "$@"; echo
STUB
chmod +x "$PROVLEDGER_VENV/bin/provledger"
""")
    venv = tmp_path / "venv"
    proc = _run_in(root, venv, "ask", "q")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "console:[ask][q]", "the bootstrap's own output stays off stdout"
    assert "bootstrap ran" in proc.stderr
    again = _run_in(root, venv, "why", "x")
    assert again.stdout.strip() == "console:[why][x]" and "bootstrap ran" not in again.stderr


def test_without_a_venv_a_failed_bootstrap_says_so(tmp_path: Path) -> None:
    root = _plugin(tmp_path, 'echo "no network" >&2; exit 1\n')
    proc = _run_in(root, tmp_path / "nothing-here", "ask", "q")
    assert proc.returncode == 127
    assert "bootstrap" in proc.stderr and "no network" in proc.stderr


def test_failed_tool_calls_have_a_hook() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    commands = [h["command"] for entry in hooks.get("PostToolUseFailure", []) for h in entry["hooks"]]
    assert commands and all("toolcall_failed.sh" in c for c in commands)
    script = (ROOT / "hooks" / "toolcall_failed.sh").read_text()
    assert "_hook.sh" in script and "PostToolUseFailure" in script
