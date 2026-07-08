# VHS tape for docs/media/install-tutorial.gif — the full install → test →
# acceptance walkthrough, recorded live in a real terminal.
#
# Regenerate (needs vhs/ttyd/ffmpeg on PATH, uv, network, and the plugin venv
# bootstrapped once so the six suites have their deps):
#
#   cd $(mktemp -d) && vhs <repo>/scripts/install-tutorial.tape
#
# NOTE: run from a scratch directory — the tape clones the repo fresh.
# Output paths are relative (VHS's parser rejects some absolute paths).

Output install-tutorial.gif

Set Shell "bash"
Set FontSize 14
Set Width 1250
Set Height 820
Set TypingSpeed 15ms

Hide
Type "export PATH=$PATH:~/.local/bin && cd $(mktemp -d) && clear"
Enter
Show

Type "# ── 1 · install ──────────────────────────────────────────"
Enter
Type "git clone --quiet https://github.com/yizhao95/prov_ledger.git && cd prov_ledger"
Enter
Wait@120s
Type "bash scripts/bootstrap.sh"
Enter
Wait@600s
Type `python3 -c "import sys; print('python', sys.version.split()[0])" && PY=$HOME/skill-workspace/.venv/bin/python && echo venv: $PY`
Enter
Wait@30s

Type `rm -rf /tmp/pl-lib && uv venv -q /tmp/pl-lib --python 3.13 && uv pip install -q --python /tmp/pl-lib/bin/python provledger && /tmp/pl-lib/bin/python -c "import provledger; print('library install from PyPI ok:', provledger.__version__)"`
Enter
Wait@180s

Type "# ── 2 · test (six suites, run separately) ────────────────"
Enter
Type "$PY -m pytest orchestrator-backend -q | tail -1"
Enter
Wait@300s
Type "$PY -m pytest orchestrator-webapp -q | tail -1"
Enter
Wait@300s
Type "$PY -m pytest skills/writing-plans/tests -q | tail -1"
Enter
Wait@300s
Type "$PY -m pytest skills/executing-plans -q | tail -1"
Enter
Wait@300s
Type "$PY -m pytest skills/project-state-graph/scripts/tests -q | tail -1"
Enter
Wait@300s
Type "$PY -m pytest skills/update-project-state-graph/scripts/tests -q | tail -1"
Enter
Wait@300s

Type "# ── 3 · acceptance: the silent-class-drop demo ───────────"
Enter
Type "make demo"
Enter
Wait@300s
Sleep 5
