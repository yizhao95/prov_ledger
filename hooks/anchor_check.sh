#!/usr/bin/env bash
# anchor_check.sh — PreToolUse hook (Edit | Write | MultiEdit): the constraints anchored
# on the lines about to change, injected as additionalContext (DP phase 2, Task 5).
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_hook.sh" PreToolUse
