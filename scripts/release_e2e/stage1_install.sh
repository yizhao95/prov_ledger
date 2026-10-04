#!/usr/bin/env bash
# stage1_install.sh — a stranger's install.
#
# Clone into an empty directory and then do what INSTALL.md says, in the order
# it says it, with the commands it prints. Fidelity to the document is the
# whole point: if a step needs a person to improvise, that is a bug in the
# document and it is reported as a FINDING rather than smoothed over.
#
# HOME is the sandbox's, so the developer's ~/skill-workspace/.venv is invisible
# here. That matters: the demo target prefers that venv when it exists, which is
# how the 0.4.1 sandbox run (FL-143) accidentally skipped the very step that was
# broken.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

banner "STAGE 1 · a stranger's install — INSTALL.md, as written"
V_ALL=$OK

# ── §2 Clone the repository ──────────────────────────────────────────────────
step "INSTALL.md §2 · clone into an empty directory"
info "source: $E2E_CLONE_FROM"
if run_logged "$E2E_LOGS/01-clone.log" git clone --quiet "$E2E_CLONE_FROM" "$E2E_CLONE"; then
    record $OK "clone into an empty directory"
else
    record $FAIL "clone into an empty directory"
    V_ALL=$(worst "$V_ALL" $FAIL)
    echo "$V_ALL" > "$E2E_ROOT/stage1.verdict"; exit 0
fi
# The document's own clone URL is ssh. Say so if it is not usable here: a
# stranger with no key on this host cannot run the line as printed.
if [ "${E2E_CHECK_DOC_CLONE_URL:-1}" = 1 ]; then
    doc_url="$(grep -oE 'git clone [^ ]+' "$E2E_CLONE/INSTALL.md" | head -1 | awk '{print $3}')"
    case "$doc_url" in
        git@*) if ! GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8' \
                    git ls-remote "$doc_url" HEAD >/dev/null 2>&1; then
                   finding "INSTALL.md §2 prints \`git clone $doc_url\` (ssh)" \
                           "ssh to that host does not work without a key; cloned over https/local instead. A reader with no GitHub ssh key is stuck on the document's first command."
                   V_ALL=$(worst "$V_ALL" $FINDING)
               fi;;
    esac
fi

cd "$E2E_CLONE" || exit 1
info "HEAD: $(git rev-parse --short HEAD) · version: $(grep -oE '^version = .*' orchestrator-backend/pyproject.toml | head -1)"

# ── §3 Create a virtual environment ──────────────────────────────────────────
step "INSTALL.md §3 · create a virtual environment (Option A, then Option B)"
VENV="$E2E_CLONE/.venv"
if run_logged "$E2E_LOGS/02-venv-a.log" python3 -m venv "$VENV" && [ -x "$VENV/bin/python" ]; then
    record $OK "§3 Option A — python3 -m venv .venv"
    VENV_BY=A
elif grep -qi 'ensurepip' "$E2E_LOGS/02-venv-a.log"; then
    # INSTALL.md documents this failure (FL-143) and points at Option B, so
    # falling through is following the document — but it is still the reason a
    # reader's first command fails, so it is named.
    finding "INSTALL.md §3 Option A — \`python3 -m venv .venv\` fails on this host" \
            "ensurepip is missing (Debian/Ubuntu/WSL system python). INSTALL.md documents the error and offers Option B; the script took Option B. The first command of §3 is still not runnable as printed without \`sudo apt install python3-venv\`."
    V_ALL=$(worst "$V_ALL" $FINDING)
    rm -rf "$VENV"
    if ! command -v uv >/dev/null 2>&1; then
        record $BLOCKED "§3 Option B — uv is not installed, so neither option works here"
        V_ALL=$(worst "$V_ALL" $BLOCKED)
        echo "$V_ALL" > "$E2E_ROOT/stage1.verdict"; exit 0
    fi
    if run_logged "$E2E_LOGS/02-venv-b.log" uv venv --python 3.13 "$VENV"; then
        record $FINDING "§3 Option B — uv venv --python 3.13 .venv"
        VENV_BY=B
    else
        record $FAIL "§3 — neither Option A nor Option B produced a venv"
        V_ALL=$(worst "$V_ALL" $FAIL)
        echo "$V_ALL" > "$E2E_ROOT/stage1.verdict"; exit 0
    fi
else
    record $FAIL "§3 Option A — python3 -m venv .venv failed for a reason INSTALL.md does not mention"
    V_ALL=$(worst "$V_ALL" $FAIL)
    echo "$V_ALL" > "$E2E_ROOT/stage1.verdict"; exit 0
fi
PY="$VENV/bin/python"
# `source .venv/bin/activate` in a subshell would not survive to the next
# command; calling the venv's own interpreter is the same thing, explicitly.
export VIRTUAL_ENV="$VENV" PATH="$VENV/bin:$PATH"
info "interpreter: $($PY -V 2>&1) at $PY"

# ── §4 Install dependencies ──────────────────────────────────────────────────
# The document gives one pip line and one uv line as if they were
# interchangeable. Read it literally: take the pip line.
step "INSTALL.md §4 · install the dependency line the document gives"
# §4 now gives one command — `pip install -r requirements.txt` — so this runs
# exactly that. It used to transcribe a hand-written dependency list from the
# document, which drifted and then verified the copy while the reader followed the
# document: the list had no `httpx` and never installed the backend, so §5 failed
# on `No module named 'provledger'` and this check reported it as a product defect.
# One authoritative file removes the whole class.
DOC_REQ="$E2E_CLONE/requirements.txt"
if [ ! -f "$DOC_REQ" ]; then
    record $FAIL "§4 — requirements.txt, which §4 tells the reader to install, is not in the clone"
    V_ALL=$(worst "$V_ALL" $FAIL); rc=1
elif "$PY" -m pip --version >/dev/null 2>&1; then
    run_logged "$E2E_LOGS/03-deps.log" "$PY" -m pip install --quiet -r "$DOC_REQ"; rc=$?
    DEPS_BY=pip
else
    # A `uv venv` ships no pip, so §4's first line cannot run after §3's Option B.
    # §4 labels both lines by which option you took and says they are not
    # interchangeable, so taking the second one is following the document.
    info "§3 Option B's venv has no pip; taking §4's \`uv pip install\` line, which the document labels for exactly this case"
    run_logged "$E2E_LOGS/03-deps.log" env VIRTUAL_ENV="$VENV" uv pip install --quiet -r "$DOC_REQ"; rc=$?
    DEPS_BY=uv
fi
if [ $rc -eq 0 ]; then record $OK "§4 — dependencies installed with $DEPS_BY"
else record $FAIL "§4 — the documented dependency line failed"; V_ALL=$(worst "$V_ALL" $FAIL); fi

# ── §4a Put `provledger` on PATH ─────────────────────────────────────────────
# Without this the suites fail in a way that looks like a product defect: a test
# fixture imports `provledger.graph_api`, the third-party provider degrades, and
# `test_providers` reports the wrong provider list. Nothing in §4 installs the
# package, which is why §4a exists — and why this check has to follow it.
step "INSTALL.md §4a · \`provledger --help\` must run"
# requirements.txt ends with `-e ./orchestrator-backend`, so §4 already installed
# it. §4a only checks — and the check matters: without it the suites fail on an
# import and the dashboard serves "unavailable" pages instead of an error.
if run_logged "$E2E_LOGS/03a-pkg-help.log" "$VENV/bin/provledger" --help; then
    record $OK "§4a — \`provledger --help\` runs, as §4a says it must"
else
    record $FAIL "§4a — \`provledger --help\` does not run after §4; §4's install did not put the backend in"
    V_ALL=$(worst "$V_ALL" $FAIL)
fi

# ── §5 Verify the install ────────────────────────────────────────────────────
# One command per documented suite, each from the directory the document names.
# In `collect` mode nothing is executed: collection alone already proves the
# install, because a dependency the documented line forgot shows up as a
# collection error — which is exactly the gap being tested here.
step "INSTALL.md §5 · the documented suites (mode: $E2E_SUITES)"
suite() {                                  # suite <label> <rundir> <target>
    local label="$1" from="$2" target="$3" log="$E2E_LOGS/04-suite-$1.log"
    local -a extra=(-q -p no:cacheprovider)
    [ "$E2E_SUITES" = collect ] && extra+=(--collect-only)
    # The PSG_REGISTRY_* exports protect the developer's real registry from
    # anything this check runs. They must NOT reach the documented suites: a
    # suite builds its own registry under its own tmp_path and asserts the file
    # appeared there, `init_project.sh` honours the environment over the argument,
    # and the file lands in the sandbox instead — so the suite fails for a reason
    # that has nothing to do with the code under test. Proven by running the one
    # test both ways: with these set it fails `registry not written`, with them
    # unset it passes. Isolation is unaffected, because $HOME already points into
    # the sandbox and a suite that sets no path of its own lands there anyway.
    ( cd "$E2E_CLONE/$from" \
      && env -u PSG_REGISTRY_ROOT -u PSG_REGISTRY_PATH -u PSG_INDEX_PATH \
             "$PY" -m pytest "$target" "${extra[@]}" ) >"$log" 2>&1
    local rc=$?
    local tail_line
    tail_line="$(grep -oE '[0-9]+(/[0-9]+)? (tests? collected|passed)[^,=]*' "$log" | tail -1)"
    if [ $rc -eq 0 ]; then
        info "$(printf '%-26s %s' "$label" "${tail_line:-ok}")"
    else
        fail "$(printf '%-26s rc=%s' "$label" "$rc")"
        sed 's/^/      | /' <"$log" | grep -E 'Error|error|E  ' | head -6
        SUITE_BAD="${SUITE_BAD:-}$label "
    fi
}
# The suite list lives in scripts/suites.sh, read from the clone under test.
run_documented_suites() {
    local SUITES s label from target
    . "$E2E_CLONE/scripts/suites.sh"
    for s in "${SUITES[@]}"; do
        IFS='|' read -r label from target <<<"$s"
        suite "$label" "$from" "$target"
    done
}
if [ "$E2E_SUITES" = none ]; then
    record $BLOCKED "§5 — suites not run (E2E_SUITES=none); the install was not verified against them"
    V_ALL=$(worst "$V_ALL" $BLOCKED)
else
    SUITE_BAD=""
    run_documented_suites
    if [ -z "$SUITE_BAD" ]; then
        case "$E2E_SUITES" in
            collect) record $OK "§5 — all nine documented suites collect cleanly (no test executed)";;
            *)       record $OK "§5 — all nine documented suites pass";;
        esac
    else
        # A suite that cannot even be collected after following §4 to the letter
        # is a hole in §4, not a broken test. Name the missing pieces.
        miss=""
        for m in httpx provledger; do
            grep -qE "No module named '?$m" "$E2E_LOGS"/04-suite-*.log 2>/dev/null && miss="$miss $m"
        done
        if [ -n "$miss" ]; then
            finding "INSTALL.md §4's dependency line is incomplete: §5's suites need$miss, which §4 never installs" \
                    "failing suites: $SUITE_BAD— installed \`-r requirements.txt\` (which does list them) to get past it. A reader who runs §4 and then §5 sees collection errors, not a green install."
            V_ALL=$(worst "$V_ALL" $FINDING)
            run_logged "$E2E_LOGS/03b-deps-requirements.log" "$PY" -m pip install --quiet -r requirements.txt \
              || run_logged "$E2E_LOGS/03b-deps-requirements.log" env VIRTUAL_ENV="$VENV" uv pip install --quiet -r requirements.txt
            SUITE_BAD=""
            for s in "$E2E_LOGS"/04-suite-*.log; do :; done
            run_documented_suites
            if [ -z "$SUITE_BAD" ]; then record $FINDING "§5 — suites green only after installing requirements.txt"
            else record $FAIL "§5 — still failing after requirements.txt: $SUITE_BAD"; V_ALL=$(worst "$V_ALL" $FAIL); fi
        else
            record $FAIL "§5 — documented suites failed: $SUITE_BAD"
            V_ALL=$(worst "$V_ALL" $FAIL)
        fi
    fi
fi

# ── §5 make demo ─────────────────────────────────────────────────────────────
# The arc, not just the exit code: MISMATCH -> revise -> VERIFIED -> SELF-CHECK OK.
step "INSTALL.md §5 · make demo — MISMATCH -> revise -> VERIFIED -> SELF-CHECK OK"
demo_log="$E2E_LOGS/05-demo.log"
( cd "$E2E_CLONE" && make demo ) >"$demo_log" 2>&1; demo_rc=$?
check_arc() {                              # check_arc <log> <rc>
    local log="$1" rc="$2" missing=""
    grep -q 'MISMATCH' "$log"        || missing="$missing MISMATCH"
    grep -qi 'revise'  "$log"        || missing="$missing revise"
    grep -q 'VERIFIED' "$log"        || missing="$missing VERIFIED"
    grep -q 'SELF-CHECK OK' "$log"   || missing="$missing SELF-CHECK-OK"
    [ "$rc" -eq 0 ]                  || missing="$missing exit-0(got=$rc)"
    echo "$missing"
}
missing="$(check_arc "$demo_log" "$demo_rc")"
if [ -z "$missing" ]; then
    record $OK "make demo — MISMATCH -> revise -> VERIFIED -> SELF-CHECK OK, exit 0"
elif grep -q 'could not create a venv' "$demo_log"; then
    # The Makefile builds its own venv with `python3 -m venv` and never looks at
    # the one INSTALL.md §3 just told the reader to create and activate.
    finding "\`make demo\` ignores the venv INSTALL.md §3 created and runs \`python3 -m venv\` itself, which fails here" \
            "the Makefile prefers \$HOME/skill-workspace/.venv (absent on a clean machine) and otherwise builds examples/phantom-uplift/.venv with the system python. Ran the demo through §3's own interpreter instead — the workaround the Makefile itself prints. On Debian/Ubuntu/WSL, \`make demo\` is not runnable as documented after a §3 Option B install."
    V_ALL=$(worst "$V_ALL" $FINDING)
    run_logged "$E2E_LOGS/05b-demo-fallback-deps.log" "$PY" -m pip install --quiet -r examples/phantom-uplift/requirements.txt \
      || run_logged "$E2E_LOGS/05b-demo-fallback-deps.log" env VIRTUAL_ENV="$VENV" uv pip install --quiet -r examples/phantom-uplift/requirements.txt
    ( cd "$E2E_CLONE" && "$PY" examples/phantom-uplift/run_demo.py ) >"$E2E_LOGS/05c-demo-direct.log" 2>&1; demo_rc=$?
    missing="$(check_arc "$E2E_LOGS/05c-demo-direct.log" "$demo_rc")"
    if [ -z "$missing" ]; then record $FINDING "the demo arc is intact when run with §3's interpreter directly"
    else record $FAIL "the demo arc is incomplete even directly — missing:$missing"; V_ALL=$(worst "$V_ALL" $FAIL); fi
else
    record $FAIL "make demo — missing:$missing"
    sed 's/^/      | /' <"$demo_log" | tail -20
    V_ALL=$(worst "$V_ALL" $FAIL)
fi

printf '\n  stage 1 verdict: %s%s%s\n' "$C_B" "$(verdict_name "$V_ALL")" "$C_0"
echo "$V_ALL" > "$E2E_ROOT/stage1.verdict"
# The clone's interpreter is what stages 2 and 3 run on: the release under test,
# installed the way a stranger would have installed it.
echo "$PY" > "$E2E_ROOT/stage1.python"
exit 0
