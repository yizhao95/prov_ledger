-- 027_export_log.sql — DP phase 3: what left the machine, and what was refused.
--
-- `provledger export` hands part of the ledger to someone who was not there.
-- That is the one operation with no undo: a file that has left has left. So the
-- export writes its own record, in the same append-only style as the three
-- chains — one row per bundle, naming the counts, the rows that were refused
-- entry, the chain heads at the time and the git anchor the bundle quotes.
--
-- `included_rationale_json` is the column this table exists for. A rationale is
-- somebody's reasoning about people, so it never travels by default; a person
-- may release one, and then the release is itself on the record, by reason id,
-- forever. "Who decided this could leave" has an answer.
--
-- No UPDATE and no DELETE: an export that happened cannot be un-happened, and a
-- log that can be edited is not a log.
CREATE TABLE IF NOT EXISTS export_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT NOT NULL,
    out_dir        TEXT NOT NULL,           -- where the bundle was written
    fmt            TEXT NOT NULL,           -- md | json | zip
    included_rationale_json TEXT NOT NULL DEFAULT '[]',   -- the reason ids somebody released, one by one
    counts_json    TEXT NOT NULL DEFAULT '{}',            -- what travelled, per chain
    skipped_json   TEXT NOT NULL DEFAULT '{}',            -- what was refused, per reason
    chain_heads_json TEXT NOT NULL DEFAULT '{}',          -- the three heads at export time
    anchor_note_sha TEXT,                   -- the git note the bundle quotes, when there was one
    anchor_commit  TEXT,
    manifest_sha256 TEXT,                   -- the manifest the bundle carries, so the two can be compared
    exported_by    TEXT NOT NULL DEFAULT 'agent' CHECK (exported_by IN ('human', 'agent', 'system')),
    exported_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_export_log_project ON export_log (project, exported_at);

CREATE TRIGGER IF NOT EXISTS trg_export_log_no_update BEFORE UPDATE ON export_log
BEGIN SELECT RAISE(ABORT, 'export_log is append-only: an export that happened cannot be un-happened'); END;
CREATE TRIGGER IF NOT EXISTS trg_export_log_no_delete BEFORE DELETE ON export_log
BEGIN SELECT RAISE(ABORT, 'export_log is append-only'); END;
