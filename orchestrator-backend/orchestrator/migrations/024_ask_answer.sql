-- 024_ask_answer.sql — DP phase 2e (Task 6): the answer a SESSION model drafted.
--
-- `/ledger` is first a slash command inside a Claude Code session: the session's
-- own model drafts the summary from the fact table `provledger ask --json`
-- printed, and hands it back through `provledger ask submit`, where the same
-- checks as the headless path delete every sentence that does not cite or that
-- carries a number the table never stated.
--
-- `ask_log` is append-only, so a second draft is not an UPDATE: it is a new row
-- here, with the next version number. The history of what was said about one
-- question is therefore complete — including the drafts that were rewritten.
CREATE TABLE IF NOT EXISTS ask_answer (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ask_id       INTEGER NOT NULL REFERENCES ask_log(id),
    version      INTEGER NOT NULL,
    answer       TEXT NOT NULL,
    cites_json   TEXT,
    dropped_json TEXT,
    model        TEXT NOT NULL DEFAULT 'session',
    at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    UNIQUE (ask_id, version)
);
CREATE INDEX IF NOT EXISTS idx_ask_answer_ask ON ask_answer (ask_id, version);
CREATE TRIGGER IF NOT EXISTS trg_ask_answer_no_update BEFORE UPDATE ON ask_answer
BEGIN SELECT RAISE(ABORT, 'ask_answer is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ask_answer_no_delete BEFORE DELETE ON ask_answer
BEGIN SELECT RAISE(ABORT, 'ask_answer is append-only'); END;
