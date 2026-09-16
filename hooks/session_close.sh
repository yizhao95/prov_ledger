#!/usr/bin/env bash
# session_close.sh — Stop hook: record the session; queue a background graph refresh when the
# session published no plan (the degraded, hooks-only mode — DP phase 2, Task 7b). stdout empty.
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_hook.sh" Stop
