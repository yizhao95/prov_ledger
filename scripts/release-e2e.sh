#!/usr/bin/env bash
# release-e2e.sh — the check that runs before every release (FL-144).
#
# It exists because two defects this week walked past a full green test run and
# were each found only by doing the thing:
#
#   · `python3 -m venv .venv`, the first command in INSTALL.md, fails on
#     Debian/Ubuntu/WSL. No test on a development machine can catch it: the venv
#     is already there, so the line never runs (FL-143).
#   · The dashboard did nothing when clicked, for a week, while 267 webapp tests
#     stayed green — every one of them renders on the server and none drives a
#     browser.
#
# So this script does only the part the suites structurally cannot reach.
#
#   Stage 1  a stranger's install: clone into an empty directory and follow
#            INSTALL.md as written, ending at `make demo`. Every place the
#            script must deviate from the document to succeed is reported as a
#            FINDING, because it is where a new reader gets stuck.
#   Stage 2  a dummy project built from scratch, then all three ways in:
#            `provledger ask`, `provledger receipts`, and the dashboard driven
#            by a real browser.
#   Stage 3  a model marks the answers against key points written down in
#            advance — whether the answer is RIGHT, which no existing assertion
#            can tell.
#
# Usage:  bash scripts/release-e2e.sh [options]
#   --suites full|collect|none   stage 1's documented suites. `full` runs all
#                                nine (the release setting, ~20 min); `collect`
#                                only collects them, which still catches a
#                                dependency the docs forgot; `none` reports the
#                                suites as BLOCKED. Default: full.
#   --clone-from <url|path>      what stage 1 clones. Default: this working
#                                tree — before a release, that is the thing
#                                being released. Pass the GitHub URL to check
#                                what is already published.
#   --stage 1|2|3                run one stage only (repeatable).
#   --keep                       do not delete the sandbox; print its path.
#   --model <name>               the model stage 3 judges with.
#
# Exit code = the worst outcome:  0 OK · 2 FINDING (docs deviated from)
#                                 3 BLOCKED (a check could not run) · 1 FAIL
#
# Safety. It creates one temporary directory and removes it. HOME, ORCH_DB and
# CLAUDE_CONFIG_DIR all point inside it, so it has its own ledger and its own
# Claude configuration; it never touches ~/skill-workspace/orchestrator.db or
# the developer's registered projects, and it proves that at the end by
# comparing that file's size and checksum with what they were at the start.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
. "$HERE/release_e2e/common.sh"

SUITES=full; CLONE_FROM="$REPO"; KEEP=0; STAGES=""; MODEL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --suites)     SUITES="${2:?}"; shift 2;;
        --clone-from) CLONE_FROM="${2:?}"; shift 2;;
        --stage)      STAGES="$STAGES ${2:?}"; shift 2;;
        --model)      MODEL="${2:?}"; shift 2;;
        --keep)       KEEP=1; shift;;
        -h|--help)    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        *) echo "unknown option: $1" >&2; exit 64;;
    esac
done
case "$SUITES" in full|collect|none) ;; *) echo "--suites must be full|collect|none" >&2; exit 64;; esac
[ -n "$STAGES" ] || STAGES="1 2 3"
wants() { case " $STAGES " in *" $1 "*) return 0;; *) return 1;; esac; }

# ── what must not be touched ─────────────────────────────────────────────────
REAL_HOME="$HOME"
GUARDED="$REAL_HOME/skill-workspace/orchestrator.db"
fingerprint() { [ -f "$1" ] && { printf '%s %s' "$(stat -c %s "$1")" "$(sha256sum "$1" | cut -d' ' -f1)"; } || printf 'absent'; }
GUARD_BEFORE="$(fingerprint "$GUARDED")"
# Every name this run invents carries this token, so "did we leak into the
# developer's ledger" is answered by looking rather than by trusting.
E2E_NONCE="rele2e$(date +%y%m%d%H%M%S)"
export E2E_NONCE

# ── the sandbox ──────────────────────────────────────────────────────────────
E2E_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/provledger-release-e2e.XXXXXXXX")" || exit 1
export E2E_ROOT
E2E_LOGS="$E2E_ROOT/logs";  mkdir -p "$E2E_LOGS"
E2E_CLONE="$E2E_ROOT/clone"
E2E_HOME="$E2E_ROOT/home";  mkdir -p "$E2E_HOME/skill-workspace"
E2E_FINDINGS="$E2E_ROOT/findings.txt"; : > "$E2E_FINDINGS"
E2E_TALLY="$E2E_ROOT/tally.txt";       : > "$E2E_TALLY"
export E2E_LOGS E2E_CLONE E2E_HOME E2E_FINDINGS E2E_TALLY
export E2E_SUITES="$SUITES" E2E_CLONE_FROM="$CLONE_FROM" E2E_MODEL="$MODEL"
export E2E_REPO="$REPO"
# The developer's workspace, checked at the end by the suites' own guard
# (scripts/home_guard.py): one definition of a leak, not a second one here.
GUARD_SNAP="$E2E_ROOT/home-guard.json"
PROVLEDGER_GUARD_HOME="$REAL_HOME/skill-workspace" python3 "$HERE/home_guard.py" snapshot "$GUARD_SNAP"

cleanup() {
    local rc=$?
    if [ "$KEEP" = 1 ]; then printf '\n  sandbox kept: %s\n' "$E2E_ROOT"
    else rm -rf "$E2E_ROOT"; fi
    exit $rc
}
trap cleanup EXIT INT TERM

# Its own everything. HOME last, so REAL_HOME above is the real one.
export ORCH_DB="$E2E_HOME/skill-workspace/orchestrator.db"
export PROVLEDGER_VENV="$E2E_HOME/skill-workspace/.venv"
# ORCH_DB alone is not enough. The state-graph registry has its own three
# paths. init_project.sh puts the index beside PSG_REGISTRY_PATH when only that
# is set; all three are still exported here as belt and braces.
export PSG_REGISTRY_ROOT="$E2E_HOME/skill-workspace/project-graphs"
export PSG_REGISTRY_PATH="$PSG_REGISTRY_ROOT/projects.json"
export PSG_INDEX_PATH="$PSG_REGISTRY_ROOT/PROJECT-STATE-GRAPHS.md"
mkdir -p "$PSG_REGISTRY_ROOT"
export PROVLEDGER_HOOK_ERRORS="$E2E_ROOT/hook-errors.log"
export PROVLEDGER_DASH_LOG="$E2E_LOGS/dashboard.log"
export CLAUDE_CONFIG_DIR="$E2E_ROOT/claude-config"; mkdir -p "$CLAUDE_CONFIG_DIR"
# Caches stay in the real home: they hold no project data, and re-downloading a
# 180 MB browser on every release run would make nobody run this.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$REAL_HOME/.cache/uv}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$REAL_HOME/.cache/pip}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$REAL_HOME/.cache/ms-playwright}"
# The stages run with HOME inside the sandbox, so `~` no longer finds the
# developer's plugin venv. Stage 2 needs to be able to look there for one thing
# only — an interpreter that can import playwright, which the documented
# dependency line deliberately does not install.
export E2E_REAL_HOME="$REAL_HOME"
export HOME="$E2E_HOME"
unset VIRTUAL_ENV

# A headless `claude` in a fresh config dir is "Not logged in". Seed the new
# directory with the credential file and the account stanza only, and write our
# own settings.json — the isolated-settings idea from
# orchestrator/testing/claude_arbiter.py, one level up: the host's language
# preference and the host's plugins must not get into a tool call's answer.
seed_claude_config() {
    [ -f "$REAL_HOME/.claude/.credentials.json" ] && cp "$REAL_HOME/.claude/.credentials.json" "$CLAUDE_CONFIG_DIR/"
    python3 - "$REAL_HOME/.claude/.claude.json" "$CLAUDE_CONFIG_DIR/.claude.json" <<'PY' 2>/dev/null || true
import json, sys
src, dst = sys.argv[1], sys.argv[2]
try:
    with open(src, encoding="utf-8") as f: d = json.load(f)
except (OSError, ValueError): d = {}
keep = {k: d[k] for k in ("oauthAccount", "userID", "installMethod", "firstStartTime") if k in d}
keep["hasCompletedOnboarding"] = True
with open(dst, "w", encoding="utf-8") as f: json.dump(keep, f)
PY
    printf '{"language":"en","enabledPlugins":{}}' > "$CLAUDE_CONFIG_DIR/settings.json"
    export PROVLEDGER_CLAUDE_SETTINGS="$CLAUDE_CONFIG_DIR/settings.json"
}
seed_claude_config

banner "provLedger release end-to-end check"
info "sandbox      $E2E_ROOT"
info "HOME         $HOME            (the developer's is $REAL_HOME)"
info "ORCH_DB      $ORCH_DB"
info "claude conf  $CLAUDE_CONFIG_DIR"
info "guarded      $GUARDED  [$GUARD_BEFORE]"
info "stages       $STAGES · suites: $SUITES"

V1=$OK; V2=$OK; V3=$OK
# A clone carries committed history only. Say so when the tree is dirty, so
# nobody reads a green stage 1 as covering edits that are not in it yet.
if [ "$CLONE_FROM" = "$REPO" ] && [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
    printf '  %sNOTE%s    the working tree is dirty; stage 1 clones committed HEAD, so uncommitted\n' "$C_YEL" "$C_0"
    info "        changes to INSTALL.md, the Makefile or the suites are NOT covered by this run."
fi
if wants 1; then
    bash "$HERE/release_e2e/stage1_install.sh"
    V1="$(cat "$E2E_ROOT/stage1.verdict" 2>/dev/null || echo $FAIL)"
fi

# Stages 2 and 3 run against the freshly installed clone when stage 1 built one
# — the release as a stranger would have it — and otherwise against this tree
# with the plugin venv, so a single `--stage 2` still works.
PY_E2E="$(cat "$E2E_ROOT/stage1.python" 2>/dev/null || true)"
if [ -z "$PY_E2E" ] || [ ! -x "$PY_E2E" ]; then
    PY_E2E="$REAL_HOME/skill-workspace/.venv/bin/python"; [ -x "$PY_E2E" ] || PY_E2E="$(command -v python3)"
    UNDER_TEST="$REPO"
else
    UNDER_TEST="$E2E_CLONE"
fi
export E2E_PYTHON="$PY_E2E" E2E_UNDER_TEST="$UNDER_TEST"

if wants 2; then
    "$PY_E2E" "$HERE/release_e2e/stage2_surfaces.py"
    V2="$(cat "$E2E_ROOT/stage2.verdict" 2>/dev/null || echo $FAIL)"
fi
if wants 3; then
    "$PY_E2E" "$HERE/release_e2e/stage3_judge.py"
    V3="$(cat "$E2E_ROOT/stage3.verdict" 2>/dev/null || echo $FAIL)"
fi

# ── the summary ──────────────────────────────────────────────────────────────
banner "SUMMARY"
wants 1 && printf '  stage 1  %-8s  a stranger'\''s install (INSTALL.md as written)\n' "$(verdict_name "$V1")"
wants 2 && printf '  stage 2  %-8s  dummy project · ask · receipts · dashboard in a real browser\n' "$(verdict_name "$V2")"
wants 3 && printf '  stage 3  %-8s  a model marks the answers against key points\n' "$(verdict_name "$V3")"

if [ -s "$E2E_FINDINGS" ]; then
    printf '\n  %s%sFINDINGS — a reader following the documents is stuck at each of these%s\n' "$C_B" "$C_YEL" "$C_0"
    awk '!seen[$0]++' "$E2E_FINDINGS" | nl -ba -w4 -s'  ' | sed 's/^/  /'
fi

# ── did anything of ours reach the developer's files? ────────────────────────
# Two separate questions, because they have two different answers.
#
# (1) Contamination — the one that matters, and it is decidable: are there rows
#     in the developer's ledger belonging to a project this run invented? Asked
#     structurally rather than by searching the file's text, because the text of
#     the sandbox path IS in there — written by the provLedger hooks of the
#     session that launched the check, which is the session's own words and not
#     the check's rows.
# (2) Whether the developer's workspace shows what a leak changes and the
#     session's hooks do not: the projects its registry and index list, the
#     newest plan / reason / question row, new entries at its top
#     (scripts/home_guard.py, the same guard run_tests.sh puts around every
#     suite). Hashing the registry and the index is not that: the Stop hook of
#     the session that launched this rewrites both on every graph refresh.
# (3) Whether the ledger file changed at all. Expected, for the same reason.
#     Reported, never failed on.
V_GUARD=$OK
leak="$(python3 "$HERE/release_e2e/leakcheck.py" "$GUARDED" "dummy-rollup-" 2>/dev/null)"
GUARD_AFTER="$(fingerprint "$GUARDED")"
echo
if [ -n "$leak" ]; then
    printf '  %sFAIL%s    this run leaked into the developer'\''s ledger:\n' "$C_RED" "$C_0"
    printf '%s\n' "$leak" | sed 's/^/            /'
    V_GUARD=$FAIL
else
    printf '  %sPASS%s    nothing of this run is in %s\n' "$C_GRN" "$C_0" "$GUARDED"
    info "        (opened read-only; no row of any table belongs to a dummy-rollup-* project)"
fi
if guard_out="$(PROVLEDGER_GUARD_HOME="$REAL_HOME/skill-workspace" python3 "$HERE/home_guard.py" check "$GUARD_SNAP")"; then
    printf '  %sPASS%s    the developer'\''s workspace shows nothing of this run\n' "$C_GRN" "$C_0"
    info "        (same projects in its registry and index, no plan / reason / question row outside a session, nothing new at its top)"
else
    printf '  %sFAIL%s    this run wrote to the developer'\''s workspace:\n' "$C_RED" "$C_0"
    printf '%s\n' "$guard_out" | sed 's/^/            /'
    V_GUARD=$FAIL
fi
if [ "$GUARD_BEFORE" != "$GUARD_AFTER" ]; then
    printf '  %sNOTE%s    %s changed while this ran, but carries nothing of ours.\n' "$C_YEL" "$C_0" "$(basename "$GUARDED")"
    info "        Expected when the check is launched from inside a Claude Code session:"
    info "        the provLedger hooks record that session's own words. Not a failure."
fi

FINAL="$(worst "$V1" "$V2" "$V3" "$V_GUARD")"
printf '\n  %sVERDICT: %s%s  (exit %s)\n\n' "$C_B" "$(verdict_name "$FINAL")" "$C_0" "$FINAL"
exit "$FINAL"
