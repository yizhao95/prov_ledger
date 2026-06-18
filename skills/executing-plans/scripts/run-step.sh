#!/usr/bin/env bash
# run-step.sh — bundled start-step + shell exec + complete/fail-step with
# auto-captured log_context. ELIMINATES the "forgot to capture logs" footgun
# the same way migration 006 eliminated "forgot to call finish-plan".
#
# Input JSON shape (see update-input.schema.json for the canonical schema):
#   {
#     "step_id": "my-plan-A",     # required
#     "type":    "COMMAND",        # required — one of the step_type enum values
#     "command": "pytest tests/",  # required — passed to `bash -c "$command"`
#     "summary": "...",            # optional — defaults to "run-step: <cmd-80>"
#     "allow_nonzero": false       # optional bool — reserved for future; v1 must be false/absent
#   }
#
# Behavior:
#   1. start-step with "[run-step] kickoff" banner as initial log_context
#   2. bash -c "$command" 2>&1 | tee tmpfile     (PIPESTATUS[0] = true exit)
#   3. truncate captured output to <=16 KiB via _truncate_log.py
#   4. append "--- exit_code=<n>, runtime=<s>s ---" footer
#   5. if exit==0: complete-step (with log_context + summary)
#      else:       fail-step    (with log_context + reason)
#   6. exit with the wrapped command's exit code (so caller sees pass/fail)
#
# Env: ORCH_DB overrides SQLite path (default: ~/skill-workspace/orchestrator.db).
set -uo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: run-step.sh <input.json|yaml|yml>" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Resolve a Python interpreter: explicit PYBIN -> repo-local .venv ->
# internal workspace venv -> system python3. Self-contained for a fresh clone.
if [[ -z "${PYBIN:-}" ]]; then
    for _cand in "${SCRIPT_DIR}/../../../.venv/bin/python" "${HOME}/skill-workspace/orchestrator/.venv/bin/python" "$(command -v python3 || true)"; do
        if [[ -n "${_cand}" && -x "${_cand}" ]]; then PYBIN="${_cand}"; break; fi
    done
fi
INPUT_PATH="$1"

# ---- parse input via Python (handles JSON or YAML, validates) ----
PARSED=$("${PYBIN}" - "${INPUT_PATH}" <<'PYEOF'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
raw = path.read_text()
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    try:
        import yaml
        data = yaml.safe_load(raw)
    except Exception as e:
        sys.stderr.write(f"run-step: cannot parse input ({e})\n")
        sys.exit(2)

for required in ("step_id", "type", "command"):
    if required not in data or not data[required]:
        sys.stderr.write(f"run-step: input missing required field '{required}'\n")
        sys.exit(2)

if data.get("allow_nonzero"):
    sys.stderr.write("run-step: 'allow_nonzero=true' is reserved for future use (v1 only allows false/absent)\n")
    sys.exit(2)

cmd_for_summary = data["command"].replace("\n", " ")[:80]
summary = data.get("summary") or f"run-step: {cmd_for_summary}"

# Emit shell-safe key=value lines that the bash side will `eval` into vars.
# Using base64 for COMMAND/SUMMARY to avoid quoting hell with arbitrary content.
import base64
def b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")

print(f"STEP_ID={data['step_id']}")
print(f"STEP_TYPE={data['type']}")
print(f"CMD_B64={b64(data['command'])}")
print(f"SUMMARY_B64={b64(summary)}")
PYEOF
)
PARSE_RC=$?
if [[ $PARSE_RC -ne 0 ]]; then
    exit $PARSE_RC
fi
eval "${PARSED}"
COMMAND="$(echo "${CMD_B64}" | base64 -d)"
SUMMARY="$(echo "${SUMMARY_B64}" | base64 -d)"

# ---- 1. start-step with kickoff banner ----
KICKOFF_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CMD_PREVIEW="${COMMAND:0:80}"
KICKOFF_BANNER="[run-step] kickoff ${KICKOFF_TS} :: ${CMD_PREVIEW}"

START_JSON="$(${PYBIN} -c "
import json, sys
print(json.dumps({
    'step_id': sys.argv[1],
    'type':    sys.argv[2],
    'log_context': sys.argv[3],
}))
" "${STEP_ID}" "${STEP_TYPE}" "${KICKOFF_BANNER}")"
START_TMP="${TMPDIR:-/tmp}/orch-rs-start-$$-${RANDOM}.json"
trap 'rm -f "${START_TMP}" "${LOG_TMP:-}" "${COMPLETE_TMP:-}"' EXIT
printf '%s' "${START_JSON}" > "${START_TMP}"

if ! "${SCRIPT_DIR}/start-step.sh" "${START_TMP}" >/dev/null; then
    echo "run-step: start-step failed for ${STEP_ID}" >&2
    exit 3
fi

# ---- 2. run the wrapped command, tee combined stdout+stderr ----
LOG_TMP="${TMPDIR:-/tmp}/orch-rs-log-$$-${RANDOM}.log"
START_NS=$(date +%s)
set +e
set -o pipefail
bash -c "${COMMAND}" 2>&1 | tee "${LOG_TMP}"
EXIT_CODE=${PIPESTATUS[0]}
set +o pipefail
set -e
END_NS=$(date +%s)
RUNTIME=$(( END_NS - START_NS ))

# ---- 3. truncate + 4. footer ----
TRUNCATED_LOG="$(${PYBIN} "${SCRIPT_DIR}/_truncate_log.py" < "${LOG_TMP}")"
FOOTER=$'\n--- exit_code='"${EXIT_CODE}"', runtime='"${RUNTIME}"$'s ---'
FINAL_LOG="${TRUNCATED_LOG}${FOOTER}"

# ---- 5. complete-step OR fail-step ----
COMPLETE_TMP="${TMPDIR:-/tmp}/orch-rs-fin-$$-${RANDOM}.json"
if [[ $EXIT_CODE -eq 0 ]]; then
    "${PYBIN}" -c "
import json, sys
print(json.dumps({
    'step_id': sys.argv[1],
    'summary': sys.argv[2],
    'log_context': sys.argv[3],
}))
" "${STEP_ID}" "${SUMMARY}" "${FINAL_LOG}" > "${COMPLETE_TMP}"
    "${SCRIPT_DIR}/complete-step.sh" "${COMPLETE_TMP}" >/dev/null
else
    "${PYBIN}" -c "
import json, sys
print(json.dumps({
    'step_id': sys.argv[1],
    'reason':  f'exit_code={sys.argv[2]}',
    'log_context': sys.argv[3],
}))
" "${STEP_ID}" "${EXIT_CODE}" "${FINAL_LOG}" > "${COMPLETE_TMP}"
    "${SCRIPT_DIR}/fail-step.sh" "${COMPLETE_TMP}" >/dev/null
fi

# ---- 6. one-line JSON summary on stdout + exit with wrapped command's code ----
"${PYBIN}" -c "
import json, sys
print(json.dumps({
    'ok': True,
    'op': 'run-step',
    'step_id': sys.argv[1],
    'exit_code': int(sys.argv[2]),
    'log_chars': int(sys.argv[3]),
    'truncated': sys.argv[4] == 'true',
}))
" "${STEP_ID}" "${EXIT_CODE}" "${#FINAL_LOG}" \
    "$([[ ${#FINAL_LOG} -gt 16500 ]] && echo true || echo false)"

exit $EXIT_CODE
