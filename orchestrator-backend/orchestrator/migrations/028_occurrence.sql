-- 028_occurrence.sql — DP phase 4: the numbers that live in decks and reports.
--
-- Spec §9. A figure on a slide is not a string on a slide: it is a reading of
-- `metric:q3_conv`, of `orders.net_revenue`, or of a figure somebody computed
-- by hand and declared (026). The node's identity is that DATA SOURCE, and the
-- deck is one place the number turned up. Rename the deck, ship a v2, send it
-- to a client — the node's history is untouched, because the file was never
-- the subject.
--
-- `node_key` here therefore holds the data-source identity — `metric:<name>`,
-- `<dataset>.<column>`, `declared:<slug>` — and not the analyser's `nk_…`
-- content hash. Those two answer different questions, and a figure in a deck
-- has an answer to the first one before any analysis run has ever seen it.
--
-- Three tables, three shapes of truth:
--
--   artifact_file   a file this project has read, by path and sha256. One
--                   column may change (`last_seen`); the path, the hash and
--                   the kind are what the row IS.
--   occurrence      somebody saw this value, at this locator, in that file, at
--                   that moment. Append-only and hash-chained like utterance /
--                   reference / change_reason, and `observed` is the only tier
--                   it can carry: a person read a number off a page. Nothing
--                   here is ever inferred — an inferred figure is a declared
--                   node (026, node_type `manual_figure`), never an occurrence.
--   anchor_state    what a check found, each time it looked. Append-only, so
--                   `anchor_lost` is a dated verdict with a reason rather than
--                   an opinion the next check can quietly reverse.
--
-- The veto item (F4) lives in the shape of these tables: an occurrence's
-- locator can NEVER be updated. A locator that stops holding its value is
-- reported lost — it is never re-pointed at wherever the number went. Pointing
-- at the wrong place is worse than admitting the pointer broke.

CREATE TABLE IF NOT EXISTS artifact_file (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL,
    path       TEXT NOT NULL,                -- as the person named it, relative to the repo when it is inside one
    sha256     TEXT NOT NULL,                -- the bytes at the moment it was read
    kind       TEXT NOT NULL CHECK (kind IN ('pptx', 'xlsx', 'docx', 'csv', 'text')),
    first_seen TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    last_seen  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_artifact_file_project ON artifact_file (project, path, sha256);

CREATE TRIGGER IF NOT EXISTS trg_artifact_file_update_whitelist BEFORE UPDATE ON artifact_file
WHEN NEW.id IS NOT OLD.id OR NEW.project IS NOT OLD.project OR NEW.path IS NOT OLD.path
  OR NEW.sha256 IS NOT OLD.sha256 OR NEW.kind IS NOT OLD.kind OR NEW.first_seen IS NOT OLD.first_seen
BEGIN SELECT RAISE(ABORT, 'artifact_file: only last_seen may change — a different path or a different sha256 is a different row'); END;
CREATE TRIGGER IF NOT EXISTS trg_artifact_file_no_delete BEFORE DELETE ON artifact_file
BEGIN SELECT RAISE(ABORT, 'artifact_file is append-only'); END;

CREATE TABLE IF NOT EXISTS occurrence (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project      TEXT NOT NULL,
    node_key     TEXT NOT NULL,              -- the data source: metric:<name> | <dataset>.<column> | declared:<slug>
    file_id      INTEGER NOT NULL REFERENCES artifact_file(id),
    locator_json TEXT NOT NULL,              -- {kind, slide|sheet|paragraph|row|line, shape?|cell?|col?, at}
    value_text   TEXT NOT NULL,              -- the value as it is written in the file
    value_num    REAL,                       -- the same value as a number, when it is one
    seen_at      TEXT NOT NULL,              -- when the file said this
    tier         TEXT NOT NULL DEFAULT 'observed' CHECK (tier = 'observed'),
    -- the single-value CHECK is the feature: nothing asserted, stated or derived
    -- can enter this table, so "a number in a deck" and "a number somebody
    -- claimed" can never be read off the same row
    by           TEXT NOT NULL DEFAULT 'human' CHECK (by IN ('human', 'agent', 'system')),
    recorded_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    prev_hash    TEXT,
    hash         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_occurrence_node ON occurrence (project, node_key, seen_at);
CREATE INDEX IF NOT EXISTS idx_occurrence_file ON occurrence (file_id);

CREATE TRIGGER IF NOT EXISTS trg_occurrence_no_update BEFORE UPDATE ON occurrence
BEGIN SELECT RAISE(ABORT, 'occurrence is append-only: a locator is never re-pointed, and a new reading is a new row'); END;
CREATE TRIGGER IF NOT EXISTS trg_occurrence_no_delete BEFORE DELETE ON occurrence
BEGIN SELECT RAISE(ABORT, 'occurrence is append-only'); END;

CREATE TABLE IF NOT EXISTS anchor_state (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    occurrence_id INTEGER NOT NULL REFERENCES occurrence(id),
    state         TEXT NOT NULL CHECK (state IN ('ok', 'anchor_lost')),
    -- two states, on purpose: there is no 'moved'. Where the number went is a
    -- question this table refuses to answer, because answering it wrongly is
    -- how a ledger starts citing the wrong slide.
    checked_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    reason        TEXT                        -- why it is lost, in words, whenever it is
);
CREATE INDEX IF NOT EXISTS idx_anchor_state_occurrence ON anchor_state (occurrence_id, checked_at);

CREATE TRIGGER IF NOT EXISTS trg_anchor_state_no_update BEFORE UPDATE ON anchor_state
BEGIN SELECT RAISE(ABORT, 'anchor_state is append-only: a check that happened found what it found'); END;
CREATE TRIGGER IF NOT EXISTS trg_anchor_state_no_delete BEFORE DELETE ON anchor_state
BEGIN SELECT RAISE(ABORT, 'anchor_state is append-only'); END;
