#!/usr/bin/env bash
# toolcall.sh — PostToolUse hook: one tool_call_log row per tool call (DP phase 0).
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_hook.sh" PostToolUse
