#!/usr/bin/env bash
# run_tests.sh — run, count or list the test suites named in scripts/suites.sh.
#
#   bash scripts/run_tests.sh                    run every suite, each in its own pytest
#   bash scripts/run_tests.sh backend webapp     run the named suites
#   bash scripts/run_tests.sh --count [name…]    collect only and print the counts
#   bash scripts/run_tests.sh --list             print the suite names
#   bash scripts/run_tests.sh psg -- -x -k foo   extra pytest arguments go after --
#
# --count is the only source of a test count: the number in any document comes
# from here, never from memory. Nothing is executed in that mode.
#
# Interpreter: $PYBIN, else the active virtualenv, else the unified venv
# (${PROVLEDGER_VENV:-~/skill-workspace/.venv}), else python3.
#
# Every suite is checked for writes to the real ~/skill-workspace
# (scripts/home_guard.py): a suite that leaks there fails even if its tests pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=suites.sh
. "$ROOT/scripts/suites.sh"

if [[ -n "${PYBIN:-}" ]]; then
    PY="$PYBIN"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
    PY="$VIRTUAL_ENV/bin/python"
elif [[ -x "${PROVLEDGER_VENV:-$HOME/skill-workspace/.venv}/bin/python" ]]; then
    PY="${PROVLEDGER_VENV:-$HOME/skill-workspace/.venv}/bin/python"
else
    PY="$(command -v python3 || true)"
    echo "run_tests.sh: no virtualenv found, using ${PY:-nothing} (run scripts/bootstrap.sh)" >&2
fi

mode=run
names=()
extra=()
while (($#)); do
    case "$1" in
        --count) mode=count ;;
        --list)  mode=list ;;
        --)      shift; extra=("$@"); break ;;
        -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)       names+=("$1") ;;
    esac
    shift
done

if [[ $mode == list ]]; then
    for s in "${SUITES[@]}"; do echo "${s%%|*}"; done
    exit 0
fi

selected=()
if ((${#names[@]} == 0)); then
    selected=("${SUITES[@]}")
else
    for n in "${names[@]}"; do
        hit=""
        for s in "${SUITES[@]}"; do [[ ${s%%|*} == "$n" ]] && hit="$s"; done
        if [[ -z $hit ]]; then
            echo "run_tests.sh: unknown suite '$n' (known: $(bash "$0" --list | tr '\n' ' '))" >&2
            exit 2
        fi
        selected+=("$hit")
    done
fi

# A suite builds its own registry under tmp_path; an exported PSG_REGISTRY_*
# redirects it and the suite fails for a reason unrelated to the code.
pytest_in() {                   # pytest_in <rundir> <args…>
    local from="$1"; shift
    (cd "$ROOT/$from" && env -u PSG_REGISTRY_ROOT -u PSG_REGISTRY_PATH -u PSG_INDEX_PATH \
        "$PY" -m pytest "$@")
}

GUARD="$ROOT/scripts/home_guard.py"
GUARD_PY="$(command -v python3 || echo "$PY")"     # stdlib only; never the suite's interpreter
SNAP="$(mktemp)"
trap 'rm -f "$SNAP"' EXIT

total=0
failed=()
for s in "${selected[@]}"; do
    IFS='|' read -r name from target <<<"$s"
    if [[ $mode == count ]]; then
        # pytest prints "N tests collected" or "N/M tests collected (K deselected)"
        n="$(pytest_in "$from" "$target" --collect-only -q -p no:cacheprovider 2>/dev/null \
            | grep -oE '[0-9]+(/[0-9]+)? tests? collected' | tail -1 | grep -oE '^[0-9]+')"
        printf '%-20s %5s\n' "$name" "${n:-0}"
        total=$((total + ${n:-0}))
    else
        echo "== $name: $from/$target"
        "$GUARD_PY" "$GUARD" snapshot "$SNAP"
        ok=1
        pytest_in "$from" "$target" -q "${extra[@]}" || ok=0
        "$GUARD_PY" "$GUARD" check "$SNAP" || ok=0
        ((ok)) || failed+=("$name")
    fi
done

if [[ $mode == count ]]; then
    printf '%-20s %5s\n' "TOTAL" "$total"
elif ((${#failed[@]})); then
    echo "FAILED suites: ${failed[*]}"
    exit 1
else
    echo "all ${#selected[@]} suite(s) passed"
fi
