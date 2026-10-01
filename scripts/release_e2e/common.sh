# common.sh — the sandbox, the logging and the verdict arithmetic shared by the
# three release-e2e stages. Sourced, never executed.
#
# Verdicts, worst-wins. The numbers ARE the exit codes, so a release with a
# documentation deviation does not go green:
#
#   0  OK       the stage did what it claims to do
#   2  FINDING  it passed, but only after deviating from what the docs say
#   3  BLOCKED  a check could not be run at all (no model, no browser, no uv)
#   1  FAIL     a check ran and said no
#
# BLOCKED is deliberately not 0. FL-144's whole point is that a check which
# quietly skips is worse than one that fails.

OK=0; FAIL=1; FINDING=2; BLOCKED=3

# severity rank, low = better; worst() folds a set of verdicts into one
_rank() { case "$1" in 0) echo 0;; 2) echo 1;; 3) echo 2;; *) echo 3;; esac; }
worst() {
    local best=0 bestrank=0 r
    for v in "$@"; do r=$(_rank "$v"); if [ "$r" -gt "$bestrank" ]; then bestrank=$r; best=$v; fi; done
    echo "$best"
}
verdict_name() { case "$1" in 0) echo OK;; 2) echo FINDING;; 3) echo BLOCKED;; *) echo FAIL;; esac; }

# ── output ───────────────────────────────────────────────────────────────────
# Nothing is silent: every step prints its own line, and every FINDING is
# repeated into $E2E_FINDINGS so the final summary cannot lose it.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_BLU=$'\033[36m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else
    C_RED=; C_GRN=; C_YEL=; C_BLU=; C_B=; C_0=
fi

banner()  { printf '\n%s%s%s\n%s\n' "$C_B$C_BLU" "$*" "$C_0" "$(printf '%.0s─' $(seq 1 72))"; }
step()    { printf '%s·%s %s\n' "$C_BLU" "$C_0" "$*"; }
info()    { printf '    %s\n' "$*"; }
pass()    { printf '  %sPASS%s    %s\n' "$C_GRN" "$C_0" "$*"; }
fail()    { printf '  %sFAIL%s    %s\n' "$C_RED" "$C_0" "$*"; }
blocked() { printf '  %sBLOCKED%s %s\n' "$C_YEL" "$C_0" "$*"; }

# finding <what the docs say> <what had to be done instead>
# Loud on purpose: a stranger following the document is stuck exactly here.
finding() {
    printf '  %s%sFINDING%s %s\n' "$C_B" "$C_YEL" "$C_0" "$1"
    printf '            deviation: %s\n' "$2"
    printf '%s\n' "FINDING · $1 :: deviation: $2" >> "$E2E_FINDINGS"
}

# record <verdict> <label>: print the verdict and append it to the stage tally
record() {
    local v="$1"; shift
    case "$v" in
        0) pass "$*";;
        2) printf '  %sPASS%s*   %s  (with a finding, see above)\n' "$C_GRN" "$C_0" "$*";;
        3) blocked "$*";;
        *) fail "$*";;
    esac
    printf '%s %s\n' "$v" "$*" >> "$E2E_TALLY"
}

# run <logfile> <cmd...> — run a documented command, tee it, keep the rc
run_logged() {
    local log="$1"; shift
    printf '    $ %s\n' "$*" | sed 's/  */ /g'
    ( "$@" ) >"$log" 2>&1
    local rc=$?
    [ "$rc" -eq 0 ] || { printf '    rc=%s, last 25 lines of %s:\n' "$rc" "$log"; sed 's/^/      | /' <"$log" | tail -25; }
    return $rc
}
