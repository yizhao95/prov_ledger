"""Tests for scripts/run_tests.sh and scripts/suites.sh — the one list of suites.

The suite list used to be written out in five places (the README, INSTALL §5,
CLAUDE.md, count_tests.sh and twice in the release check), and each copy drifted
on its own. Now `suites.sh` is the list, `run_tests.sh` is the only way to run
or count it, and the release check reads the same file.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUITES = REPO / "scripts" / "suites.sh"
RUN = REPO / "scripts" / "run_tests.sh"
STAGE1 = REPO / "scripts" / "release_e2e" / "stage1_install.sh"


def _suites() -> list[tuple[str, str, str]]:
    out = subprocess.run(
        ["bash", "-c", f'. "{SUITES}"; printf "%s\\n" "${{SUITES[@]}}"'],
        capture_output=True, text=True, check=True).stdout
    return [tuple(line.split("|")) for line in out.splitlines() if line]


def _run(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(["bash", str(RUN), *args], capture_output=True, text=True,
                          cwd=REPO, env=env, timeout=300)


def test_the_list_names_nine_suites_that_exist() -> None:
    suites = _suites()
    assert len(suites) == 9
    names = [name for name, _, _ in suites]
    assert len(set(names)) == len(names), "suite names are unique"
    for name, rundir, target in suites:
        assert (REPO / rundir / target).is_dir(), f"{name}: {rundir}/{target} does not exist"


def test_list_prints_every_suite() -> None:
    proc = _run("--list")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == [name for name, _, _ in _suites()]


def test_count_collects_without_running() -> None:
    proc = _run("--count", "plugin")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("plugin"))
    assert int(line.split()[-1]) > 0


def test_an_unknown_suite_is_refused() -> None:
    proc = _run("--count", "no-such-suite")
    assert proc.returncode == 2
    assert "no-such-suite" in proc.stderr


def test_registry_overrides_never_reach_a_suite() -> None:
    """A suite builds its own registry under tmp_path; an exported PSG_REGISTRY_*
    redirects it and the suite fails for a reason unrelated to the code."""
    src = RUN.read_text()
    for var in ("PSG_REGISTRY_ROOT", "PSG_REGISTRY_PATH", "PSG_INDEX_PATH"):
        assert f"-u {var}" in src, f"run_tests.sh must unset {var} for the suites"


def test_the_release_check_reads_the_same_list() -> None:
    src = STAGE1.read_text()
    assert "suites.sh" in src
    literal = re.findall(r"^\s*suite\s+[A-Za-z].*$", src, re.M)
    assert not literal, "stage1_install.sh still hardcodes suites:\n" + "\n".join(literal)


def test_a_suite_that_writes_to_the_real_workspace_fails(tmp_path: Path) -> None:
    """run_tests.sh checks the workspace around every suite (scripts/home_guard.py).
    The 'leak' here is an interpreter wrapper that writes into the guarded
    workspace whenever it is asked to run pytest."""
    ws = tmp_path / "skill-workspace"
    (ws / "project-graphs").mkdir(parents=True)
    leaky = tmp_path / "leaky-python"
    leaky.write_text(
        "#!/usr/bin/env bash\n"
        f'case " $* " in *" pytest "*) touch "{ws}/written-by-a-test";; esac\n'
        f'exec "{sys.executable}" "$@"\n')
    leaky.chmod(0o755)
    env = {"PROVLEDGER_GUARD_HOME": str(ws)}

    clean = _run("plugin", env_extra={**env, "PYBIN": sys.executable})
    assert clean.returncode == 0, clean.stdout[-2000:] + clean.stderr

    leaked = _run("plugin", env_extra={**env, "PYBIN": str(leaky)})
    assert leaked.returncode == 1
    assert "written-by-a-test" in leaked.stdout
    assert "FAILED suites: plugin" in leaked.stdout


def test_count_tests_sh_is_gone() -> None:
    assert not (REPO / "scripts" / "count_tests.sh").exists(), \
        "run_tests.sh --count replaces count_tests.sh; two scripts would be two lists"


def _copy_runner(tmp_path: Path) -> Path:
    """run_tests.sh + suites.sh in a scratch root, so `$ROOT/.venv` can be faked."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("run_tests.sh", "suites.sh"):
        (scripts / name).write_text((REPO / "scripts" / name).read_text())
    return scripts / "run_tests.sh"


def _which(runner: Path, **env: str) -> str:
    base = {k: v for k, v in os.environ.items() if k not in ("PYBIN", "VIRTUAL_ENV")}
    proc = subprocess.run(["bash", str(runner), "--which"], capture_output=True, text=True,
                          env={**base, **env}, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_the_repo_venv_comes_before_the_plugin_venv(tmp_path: Path) -> None:
    """The plugin's venv (~/skill-workspace/.venv) holds the RELEASED provledger;
    a test run must use the working tree's, from the repo's own .venv."""
    runner = _copy_runner(tmp_path)
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.symlink_to(sys.executable)
    assert _which(runner) == str(venv_py)


def test_an_explicit_choice_still_wins(tmp_path: Path) -> None:
    runner = _copy_runner(tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").symlink_to(sys.executable)
    other = tmp_path / "other-venv"
    (other / "bin").mkdir(parents=True)
    (other / "bin" / "python").symlink_to(sys.executable)
    assert _which(runner, VIRTUAL_ENV=str(other)) == str(other / "bin" / "python")
    assert _which(runner, PYBIN="/usr/bin/python3") == "/usr/bin/python3"
