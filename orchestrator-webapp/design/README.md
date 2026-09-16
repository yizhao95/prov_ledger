# provLedger Design

The dashboard's components, compiled — the form [Claude Design](https://claude.ai/code)
can read, lay out and hand back.

## Why this exists twice

Claude Design only understands compiled React components. The dashboard itself is
Jinja + HTMX and is going to stay that way: it has to run from a single
`launch_dashboard.sh` with no Node in the loop. So the look lives in two places,
and the only defence against drift is that **neither copy owns the palette**:

```
orchestrator-webapp/app/static/tokens.json     ← the one file a human edits
  ├─ scripts/gen_tokens.py ─→ app/static/tokens.js    (the Jinja chrome)
  └─ scripts/gen_tokens.py ─→ design/src/tokens.ts    (this library)
```

`python3 scripts/gen_tokens.py --check` fails the test suite when either
generated file is stale. That is the whole contract.

## The nine components

Every prop is a field `orchestrator-webapp/app/queries.py` already returns, so a
design made here is a design against real data.

| Component | Props | From |
|---|---|---|
| `TierBadge` | `tier` | `queries.tier_badge` |
| `SourceLevelBadge` | `level` | `queries.SOURCE_LEVELS` |
| `HeadlineBlock` | `summary`, `findings[]` | `queries.get_headline` |
| `TimelineEntry` | `run`, `event`, `tier`, `because`, `shown`, `adoptedBy[]`, `diff` | `queries.get_node_ledger` + `record_stats` |
| `ViewSwitcher` | `current`, `triple`, `links`, `onSearch` | `queries.view_bar` |
| `NodeBadge` | `count`, `tier`, `minor` | `queries.node_badges` |
| `SessionCard` | `sessionId`, `utterances`, `calls`, `buckets`, `changedNodes`, `plans[]`, `degraded` | `queries.get_session` |
| `OutcomeRow` | `claim`, `target`, `latest` | `queries.get_expectations_with_latest_outcome` |
| `ShownAdopted` | `shown[]`, `adopted[]` | `queries.get_shown_adopted` |

Plus the layout primitives the 2d spec asks for: `Column` (one column, 880px),
`RailRow` / `RailEnd` (one row per node on a line, with a start and an end),
`Disclose` (expand in place, never navigate), `BeforeAfter` (a change shown as
before → after, not the word "changed"), `Hidden` (an id the reader does not
need, kept in the tooltip).

## Build

```bash
cd orchestrator-webapp/design
npm ci
npm run build      # dist/_ds_bundle.js + _ds_bundle.css + styles.css
npm test           # structural checks: exports, preview cards, style closure
```

`dist/` is **committed**. The Claude Design sync tool reads the checked-in tree
and never runs our build.

## Preview cards

`previews/<Name>.html` — one per component, first line `<!-- @dsCard group="…" -->`,
two or three variants each, rendered from `dist/`. Open one in a browser; it
needs no server and no network.

## Porting a Claude Design change back

The rule that keeps this from becoming a second source of truth:

1. A colour, size or radius change → edit **`tokens.json`** and re-run the
   generator. Never edit `tokens.js` or `tokens.ts`.
2. A layout change → edit the component here, then mirror the class names in
   `orchestrator-webapp/app/templates/`.
3. **Never** change a prop name or shape to suit a design. The props are the
   dashboard's query results; renaming one here silently detaches the design
   from the data it claims to show.
