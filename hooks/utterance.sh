#!/usr/bin/env bash
# utterance.sh — UserPromptSubmit hook: the user's prompt, verbatim, into utterance (DP phase 1),
# and the A3 source-mention hint when that prompt named an outside source.
# stdout is at most ONE plain line, because Claude Code injects it as context: the
# hint asks for a pointer and can be ignored. It never denies anything.
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_hook.sh" UserPromptSubmit
