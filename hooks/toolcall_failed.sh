#!/usr/bin/env bash
# toolcall_failed.sh — PostToolUseFailure hook: a failed tool call is logged too, marked (FL-208).
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_hook.sh" PostToolUseFailure
