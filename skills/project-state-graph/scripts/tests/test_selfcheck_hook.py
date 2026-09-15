"""selfcheck.hook_failures — the hooks' error log is a visible count (DP phase 1)."""
import selfcheck


def test_hook_failures_counts_the_error_log(tmp_path, monkeypatch):
    log = tmp_path / "hook-errors.log"
    monkeypatch.setenv("PROVLEDGER_HOOK_ERRORS", str(log))
    c = selfcheck._check_hook_failures(None)
    assert c["name"] == "hook_failures" and c["ok"] is True and "0 failures" in c["detail"]
    log.write_text("2026-09-15T10:00:00Z UserPromptSubmit OperationalError: database is locked\n\n"
                   "2026-09-15T10:01:00Z PostToolUse ValueError: not json\n")
    c = selfcheck._check_hook_failures(None)
    assert c["ok"] is False and c["detail"].startswith("2 hook failure(s)") and "PostToolUse" in c["detail"]
    assert any(f.__name__ == "_check_hook_failures" for f in selfcheck._CHECKS)
