# VHS tape for the end-to-end journey VIDEO (terminal segment):
#   fresh clone → BRAND-NEW venv → seven test suites → install Claude Code
#   → Claude Code runs the acceptance task → dashboard up (read-only).
#
# Regenerate (needs vhs/ttyd/ffmpeg, uv, node/npm, network, and a logged-in
# Claude Code):
#   mkdir -p /tmp/pl-e2e-rec && cd /tmp/pl-e2e-rec && vhs <repo>/scripts/e2e-journey.tape
# The browser segment is recorded separately (record_dashboard-style) against
# the dashboard this tape leaves running on :8765, then concatenated.
#
# PRIVACY: keep every printed line free of user-specific paths — variables are
# typed unexpanded on purpose; the working dir is a neutral /tmp/pl-e2e.
# Output paths are relative (VHS's parser rejects some absolute paths).

Output e2e-journey-terminal.mp4

Set Shell "bash"
Set FontSize 14
Set Width 1250
Set Height 820
Set TypingSpeed 15ms

Hide
Type "export PATH=$PATH:~/.local/bin && rm -rf /tmp/pl-e2e && mkdir -p /tmp/pl-e2e && cd /tmp/pl-e2e && clear"
Enter
Show

Type "# ── 1 · fresh clone + a BRAND-NEW venv ───────────────────"
Enter
Type "git clone --quiet https://github.com/yizhao95/prov_ledger.git && cd prov_ledger"
Enter
Wait@120s
Type "export PROVLEDGER_VENV=$PWD/.venv-fresh && bash scripts/bootstrap.sh"
Enter
Wait@600s
Type "PY=$PROVLEDGER_VENV/bin/python && $PY --version"
Enter
Wait@30s

Type "# ── 2 · seven test suites, run separately, in the fresh venv"
Enter
Type "$PY -m pytest scripts/tests -q | tail -1"
Enter
Wait@120s
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

Type "# ── 3 · install Claude Code ──────────────────────────────"
Enter
Type "npm install -g @anthropic-ai/claude-code 2>&1 | tail -2 && claude --version"
Enter
Wait@300s

Type "# ── 4 · Claude Code runs the acceptance task ─────────────"
Enter
Type `claude -p "Run: make demo — then report the final SELF-CHECK line verbatim." --allowedTools "Bash(make demo)"`
Enter
Wait@600s

Type "# ── 5 · dashboard up (read-only), served from the fresh venv"
Enter
Type "ORCH_DB=$PWD/examples/phantom-uplift/demo-orchestrator.db bash orchestrator-webapp/launch_dashboard.sh"
Enter
Wait@60s
Sleep 5
