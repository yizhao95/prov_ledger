-- 030_evidence.sql — DP phase 5: which door the words came in through, and
-- what backs them.
--
-- `recorded_by` is a claim the writer makes about itself. `origin` is decided by
-- the entry point that took the words — the UserPromptSubmit hook, a person at a
-- terminal, an agent running the CLI, an import — so it is the half of "who said
-- this" that can be checked rather than trusted. Rows written before the column
-- existed read 'unknown' and stay that way: the append-only trigger refuses the
-- backfill, which is the point. We do not guess where old words came from.
--
-- A reference is a pointer, and pointers rot. reference_check records what a
-- person or a checker found when it opened one, on a date, append-only — so
-- `gone` is a dated finding with a note beside it and not an opinion the next
-- check can quietly reverse. `reference.last_checked` keeps saying WHEN it was
-- last looked at; this table says WHAT was seen, every time.
--
-- reference_link is rebuilt (SQLite cannot add a CHECK in place, so this follows
-- the create-_new / INSERT…SELECT / DROP / RENAME pattern of 015). It gains
-- utterance_id, because a source can be named in the sentence itself — before a
-- plan, and therefore before any reason, exists — and stance, because evidence
-- is allowed to disagree with the reason it is attached to. An email that
-- contradicts the reading is recorded as contradicting it; dropping it is the
-- thing this project refuses to do. Exactly one of reason_id / utterance_id is
-- set, or the same pointer would be counted twice.
--
-- change_reason_v is rebuilt from 020's body — NOT 018's — plus utterance_origin
-- (the origin of the words a `stated` row quotes, NULL for every other tier).
-- 020 replaced the 018 view and added significance_eff, and 022's node_badge_v
-- reads that column; starting from 018 would drop it and take the badge with it.
-- evidence_level and significance_eff are copied across untouched: what a tier
-- means and how checkable a record is are not what this migration is about. Both
-- views are dropped before the table swap and re-created after it, so nothing in
-- the schema points at a reference_link that does not exist at RENAME time.

-- ── Part 1 · utterance.origin ────────────────────────────────────────────────
-- A plain ADD COLUMN: no rebuild, no UPDATE. Pre-existing rows read 'unknown'
-- from the default, and trg_utterance_no_update keeps them that way forever.
ALTER TABLE utterance ADD COLUMN origin TEXT NOT NULL DEFAULT 'unknown'
    CHECK (origin IN ('hook', 'human_cli', 'agent_cli', 'import', 'unknown'));
CREATE INDEX IF NOT EXISTS idx_utterance_origin ON utterance (origin, id);

-- ── Part 2 · reference_check ─────────────────────────────────────────────────
-- Somebody opened the pointer on this date and it was there, or it was not.
-- Append-only and hash-chained like the rest of the provenance tables: a check
-- that happened found what it found.
CREATE TABLE IF NOT EXISTS reference_check (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_id INTEGER NOT NULL REFERENCES reference(id),
    checked_at   TEXT NOT NULL,                  -- when the pointer was opened
    verdict      TEXT NOT NULL CHECK (verdict IN ('ok', 'gone', 'moved', 'no_access')),
    -- 'no_access' is not 'gone': a document behind a login this checker does not
    -- hold is still there, and saying otherwise would retire a live reference.
    note         TEXT,                           -- what was seen, in words, whenever there is anything to say
    prev_hash    TEXT,
    hash         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reference_check_reference ON reference_check (reference_id, checked_at);
CREATE TRIGGER IF NOT EXISTS trg_reference_check_no_update BEFORE UPDATE ON reference_check
BEGIN SELECT RAISE(ABORT, 'reference_check is append-only: a later check is a new row'); END;
CREATE TRIGGER IF NOT EXISTS trg_reference_check_no_delete BEFORE DELETE ON reference_check
BEGIN SELECT RAISE(ABORT, 'reference_check is append-only'); END;

-- ── Part 3 · reference_link rebuilt ──────────────────────────────────────────
-- The views come down first: both of them name reference_link, and ALTER TABLE
-- … RENAME re-parses every view in the schema.
DROP VIEW IF EXISTS node_badge_v;
DROP VIEW IF EXISTS change_reason_v;

CREATE TABLE reference_link_new (
    reason_id    INTEGER REFERENCES change_reason(id),
    reference_id INTEGER NOT NULL REFERENCES reference(id),
    utterance_id INTEGER REFERENCES utterance(id),
    stance       TEXT NOT NULL DEFAULT 'supports' CHECK (stance IN ('supports', 'contradicts', 'context')),
    -- exactly one anchor: a reason, or the words that named the source
    CHECK ((reason_id IS NOT NULL) + (utterance_id IS NOT NULL) = 1)
);
INSERT INTO reference_link_new (reason_id, reference_id, utterance_id, stance)
    SELECT reason_id, reference_id, NULL, 'supports' FROM reference_link;
DROP TABLE reference_link;
ALTER TABLE reference_link_new RENAME TO reference_link;

-- 018's PRIMARY KEY (reason_id, reference_id) cannot carry the utterance half of
-- the table (SQLite lets NULLs into a composite primary key, so it would stop
-- de-duplicating exactly where it is needed). Two partial unique indexes say the
-- same thing per anchor, and keep provenance.link_reference's INSERT OR IGNORE
-- working as it did.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reference_link_reason
    ON reference_link (reason_id, reference_id) WHERE reason_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_reference_link_utterance
    ON reference_link (utterance_id, reference_id) WHERE utterance_id IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS trg_reference_link_no_update BEFORE UPDATE ON reference_link
BEGIN SELECT RAISE(ABORT, 'reference_link is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_reference_link_no_delete BEFORE DELETE ON reference_link
BEGIN SELECT RAISE(ABORT, 'reference_link is append-only'); END;

-- ── Part 4 · change_reason_v + node_badge_v back up ──────────────────────────
-- 020's body, with utterance_origin added. evidence_level and significance_eff
-- are unchanged, character for character.
CREATE VIEW change_reason_v AS
SELECT r.*,
       CASE
         WHEN EXISTS (SELECT 1 FROM reference_link l JOIN reference f ON f.id = l.reference_id
                      WHERE l.reason_id = r.id AND f.verifiability = 'linked') THEN 'linked'
         WHEN r.verbatim_utterance_id IS NOT NULL
              OR EXISTS (SELECT 1 FROM reference_link l JOIN reference f ON f.id = l.reference_id
                         WHERE l.reason_id = r.id AND f.kind = 'verbal') THEN 'verbal'
         WHEN r.tier = 'unstated' THEN 'unstated'
         ELSE 'task_context'
       END AS evidence_level,
       COALESCE(
         (SELECT s.verdict FROM significance_log s WHERE s.reason_id = r.id AND s.verdict IS NOT NULL ORDER BY s.id DESC LIMIT 1),
         (SELECT s.hint FROM significance_log s WHERE s.reason_id = r.id ORDER BY s.id DESC LIMIT 1)
       ) AS significance_eff,
       -- which door the quoted words came in through; NULL unless the row is `stated`
       (SELECT u.origin FROM utterance u WHERE u.id = r.verbatim_utterance_id) AS utterance_origin
FROM change_reason r;

-- 022's body, unchanged: the badge counts what a person or a model put there.
CREATE VIEW node_badge_v AS
SELECT r.node_key,
       r.project,
       SUM(CASE WHEN r.role = 'reason' AND r.tier IN ('stated', 'asserted')
                 AND COALESCE(r.significance_eff, 'major') <> 'minor' THEN 1 ELSE 0 END) AS reasons,
       SUM(CASE WHEN r.role = 'rejected_path' THEN 1 ELSE 0 END) AS rejected_paths,
       SUM(CASE WHEN r.role = 'constraint' AND r.state = 'active' AND r.superseded_by IS NULL THEN 1 ELSE 0 END) AS constraints,
       SUM(CASE WHEN (r.role = 'reason' AND r.tier IN ('stated', 'asserted')
                       AND COALESCE(r.significance_eff, 'major') <> 'minor')
                  OR r.role = 'rejected_path'
                  OR (r.role = 'constraint' AND r.state = 'active' AND r.superseded_by IS NULL) THEN 1 ELSE 0 END) AS badge,
       SUM(CASE WHEN r.role = 'reason' AND r.tier = 'derived' THEN 1 ELSE 0 END) AS derived,
       SUM(CASE WHEN r.role = 'reason' AND COALESCE(r.significance_eff, 'major') = 'minor' THEN 1 ELSE 0 END) AS minor,
       MAX(r.occurred_at) AS last_at
FROM change_reason_v r
WHERE r.node_key IS NOT NULL
GROUP BY r.node_key, r.project;
