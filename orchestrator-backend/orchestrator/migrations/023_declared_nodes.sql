-- 023_declared_nodes.sql — DP phase 2c: the world outside the code, in the same graph.
--
-- The third core of the product (NORTH-STAR): a user says one sentence and a
-- business rule, an external system, a stakeholder decision, an external
-- dataset or a hand-computed figure becomes a node with the same identity,
-- history, reasons, constraints and three views as a function node.
--
-- The invariant this table carries: a declared node is `stated` or `asserted`
-- and NEVER `observed`. Nobody observed a steering-group decision — somebody
-- said it, or a model tidied up what somebody said. `field_tiers_json` keeps
-- the per-field answer to "who said this" next to the row-level tier, so the
-- user's own words and the model's tidy-up never blur into one another.
--
-- Append-only like utterance / reference / change_reason: one INSERT per
-- version, `superseded_by` is the only column an UPDATE may touch, DELETE is
-- refused, and every row carries the sha256 chain that provenance.verify_chain
-- walks. A change to a declared node is a new row, never an edit.
CREATE TABLE IF NOT EXISTS declared_node (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT NOT NULL,
    slug           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,            -- 'declared:<slug>' — the name the graph knows it by
    node_type      TEXT NOT NULL CHECK (node_type IN ('external_system', 'business_rule',
                                                      'stakeholder_decision', 'external_dataset',
                                                      'manual_figure')),
    description    TEXT NOT NULL,            -- the sentence, as it was said
    attrs_json     TEXT NOT NULL DEFAULT '{}',
    links_json     TEXT NOT NULL DEFAULT '[]',   -- [{to, kind, by}] — kind in declared_feeds / declared_constrains / declared_depends_on
    links_checked  INTEGER NOT NULL DEFAULT 0 CHECK (links_checked IN (0, 1)),  -- 0 = no graph was available to check the targets against
    state          TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'active', 'retired')),
    -- observed is absent from this enum on purpose, and the absence is the feature
    tier           TEXT NOT NULL CHECK (tier IN ('stated', 'asserted')),
    field_tiers_json TEXT NOT NULL DEFAULT '{}',
    description_utterance_id INTEGER REFERENCES utterance(id),
    version        INTEGER NOT NULL DEFAULT 1,
    supersedes     INTEGER REFERENCES declared_node(id),
    superseded_by  INTEGER,
    recorded_by    TEXT NOT NULL CHECK (recorded_by IN ('human', 'agent', 'system')),
    model          TEXT,                     -- which model tidied the description up, when one did
    occurred_at    TEXT NOT NULL,
    recorded_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    prev_hash      TEXT,
    hash           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_declared_node_project ON declared_node (project, state, superseded_by);
CREATE INDEX IF NOT EXISTS idx_declared_node_slug ON declared_node (project, slug, version);
CREATE INDEX IF NOT EXISTS idx_declared_node_qn ON declared_node (qualified_name);

CREATE TRIGGER IF NOT EXISTS trg_declared_node_update_whitelist BEFORE UPDATE ON declared_node
WHEN NEW.id IS NOT OLD.id OR NEW.project IS NOT OLD.project OR NEW.slug IS NOT OLD.slug
  OR NEW.qualified_name IS NOT OLD.qualified_name OR NEW.node_type IS NOT OLD.node_type
  OR NEW.description IS NOT OLD.description OR NEW.attrs_json IS NOT OLD.attrs_json
  OR NEW.links_json IS NOT OLD.links_json OR NEW.links_checked IS NOT OLD.links_checked
  OR NEW.state IS NOT OLD.state OR NEW.tier IS NOT OLD.tier
  OR NEW.field_tiers_json IS NOT OLD.field_tiers_json
  OR NEW.description_utterance_id IS NOT OLD.description_utterance_id
  OR NEW.version IS NOT OLD.version OR NEW.supersedes IS NOT OLD.supersedes
  OR NEW.recorded_by IS NOT OLD.recorded_by OR NEW.model IS NOT OLD.model
  OR NEW.occurred_at IS NOT OLD.occurred_at OR NEW.recorded_at IS NOT OLD.recorded_at
  OR NEW.prev_hash IS NOT OLD.prev_hash OR NEW.hash IS NOT OLD.hash
BEGIN SELECT RAISE(ABORT, 'declared_node: only superseded_by may change'); END;
CREATE TRIGGER IF NOT EXISTS trg_declared_node_no_delete BEFORE DELETE ON declared_node
BEGIN SELECT RAISE(ABORT, 'declared_node is append-only'); END;
