#!/usr/bin/env bash
# utterance.sh — UserPromptSubmit hook: the user's prompt, verbatim, into utterance (DP phase 1).
# stdout MUST stay empty (it would be injected as context); _hook.sh guarantees that.
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_hook.sh" UserPromptSubmit
