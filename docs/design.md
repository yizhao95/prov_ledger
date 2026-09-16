# The dashboard's design system

The dashboard's look exists in two places and neither one owns it.

The dashboard itself is Jinja + HTMX and stays that way: it has to run from one
`launch_dashboard.sh` with no Node in the loop. Claude Design only understands
compiled React components. So there are two renderers — and the only defence
against them drifting is that **the palette lives in a third file that both are
generated from**.

```
orchestrator-webapp/app/static/tokens.json      ← the one file a human edits
  ├─ scripts/gen_tokens.py ─→ app/static/tokens.js     (the Jinja chrome)
  └─ scripts/gen_tokens.py ─→ design/src/tokens.ts     (the React library)
```

```bash
python3 scripts/gen_tokens.py            # regenerate both
python3 scripts/gen_tokens.py --check    # exit 1 if either is stale
```

`--check` runs in the test suite. It catches the one failure mode a generated
file actually has: somebody edited `tokens.json` and did not regenerate.

A drifting tier colour is not a cosmetic problem. `observed` and `asserted` are
the difference between what the system measured and what a model guessed; if
those two ever share a tint, the page is lying about provenance. That is why the
palette is a build artefact and not a convention.

## What is in tokens.json

| Section | What it holds |
|---|---|
| `colors` | the chrome, the status hues, the ink and surface tones |
| `tiers` | per tier: the label, the badge classes, the identity dot |
| `severities` | per severity: the icon and the tone |
| `type_scale`, `spacing`, `radii` | the sizes everything else refers to |
| `layout.column_max` | `880px` — the single narrative column |
| `motion.transition_ms` | `160` — the View Transition duration |

`queries.TIER_BADGES` loads from it at import, with a literal fallback that marks
itself `degraded` rather than rendering a blank dashboard.

## The component library

`orchestrator-webapp/design/` — React 18, bundled by esbuild into one IIFE on
`window.ProvLedgerDS`. Every prop is a field `orchestrator-webapp/app/queries.py`
already returns, so a design made against this library is a design against real
data rather than a mock that slowly becomes wishful thinking.

| Component | Props | Source of the shape |
|---|---|---|
| `TierBadge` | `tier` | `queries.tier_badge` |
| `SourceLevelBadge` | `level` | `queries.SOURCE_LEVELS` |
| `HeadlineBlock` | `summary`, `findings[]` | `queries.get_headline` |
| `TimelineEntry` | `run`, `event`, `tier`, `because`, `shown`, `adoptedBy[]`, `diff` | `get_node_ledger` + `record_stats` |
| `ViewSwitcher` | `current`, `triple`, `links`, `onSearch` | `queries.view_bar` |
| `NodeBadge` | `count`, `tier`, `minor` | `queries.node_badges` |
| `SessionCard` | `sessionId`, `utterances`, `calls`, `buckets`, `changedNodes`, `plans[]`, `degraded` | `queries.get_session` |
| `OutcomeRow` | `claim`, `target`, `latest` | `get_expectations_with_latest_outcome` |
| `ShownAdopted` | `shown[]`, `adopted[]` | `queries.get_shown_adopted` |

Plus the layout primitives, which are the 2d layout spec written as code rather
than prose: `Column` (one column, 880px), `RailRow` / `RailEnd` (one row per
node on a line, with a start and an end), `Disclose` (expands in place — a
decision point never navigates away), `BeforeAfter` (a change shown as
before → after, not the word "changed"), `Hidden` (an id the reader does not
need, kept in the tooltip).

```bash
cd orchestrator-webapp/design
npm ci
npm run build     # dist/_ds_bundle.js + _ds_bundle.css + styles.css
npm test          # exports, preview cards, stylesheet closure
npx tsc --noEmit  # types
```

`dist/` and `package-lock.json` are committed: the Claude Design sync reads the
checked-in tree and never runs our build.

## Preview cards

`design/previews/<Name>.html` — one per component, first line
`<!-- @dsCard group="Provenance" -->` (the switcher and the session card are
`Navigation`), two or three variants each as **static markup**, rendered from
`dist/`. Open one in a browser: no server, no network, no import map.

Variants are static on purpose. A card whose variants only exist at runtime can
lose one without the file changing, and then the design and the component quietly
disagree.

## Syncing to Claude Design

1. Run `/design-sync` in this repo.
2. Point it at `orchestrator-webapp/design/` — the package with `package.json`,
   `src/` and the built `dist/`.
3. The project is **provLedger Dashboard** (it is already recorded in
   `.design-sync/config.json`, along with `pkg`).
4. Every `previews/*.html` becomes a card, grouped by its `@dsCard` group.

## Porting a Claude Design change back

Three rules, in order of how much damage getting them wrong does:

1. **Never change a prop's name or shape to suit a design.** The props are the
   dashboard's query results. Renaming one here silently detaches the design
   from the data it claims to show, and the detachment is invisible until
   somebody trusts the picture.
2. **A colour, size, radius or duration change goes into `tokens.json`**, then
   `python3 scripts/gen_tokens.py`. Never edit `tokens.js` or `tokens.ts` —
   they are build output and the next regeneration will overwrite you.
3. **A layout change** is made in the component, then mirrored in
   `orchestrator-webapp/app/templates/`. The Jinja side is what people actually
   look at; a component that has moved on from the template is a design nobody
   is using.

Two things a redesign may never quietly remove, because they are the product
rather than the decoration:

- **the five tiers reading as five different things** — greyscale-safe, label
  first, `unstated` shown as a real tier and not blended away;
- **被看到 and 改变了计划 as two separate counts** — the system can record that
  something was shown, never that anyone read it, and a design that merges the
  two numbers is asserting something the ledger refuses to assert.

## Wording

`orchestrator-webapp/app/vocab.py` is the one table the page reads its words
from. English is the default; `?lang=zh` switches.

The register is an audit surface's: professional, precise, restrained. Short
nouns, no explanatory sentences, and the ledger's own terms used directly —
`observed`, `derived`, `asserted`, `stated`, `unstated` are already the precise
words, so they are the labels, with a one-line definition in the tooltip
(`vocab.define`). A count states a measurement: `Surfaced 3 · Adopted 2`, not a
story about who looked at what.

Two rules the suite enforces:

- **every value has an entry in both columns** — a phrase added in one language
  and left untranslated in the other fails `test_every_ui_phrase_exists_in_both_columns`;
- **machine attributes keep the ledger's token.** `data-tier="asserted"` stays
  `asserted` in every language, so the wording can change without the ETag, the
  test suite or anyone reading the page as data noticing.

An earlier draft of this table overcorrected into conversational Chinese
("因历史而变的决定", "还管着它的规矩"). That is the failure mode to avoid in both
directions: a page that chats is as unusable for audit as a page that only
prints column names.
