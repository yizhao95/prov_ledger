# Changelog

All notable changes to provLedger — the `provledger` package, the four
skills (`writing-plans`, `executing-plans`, `project-state-graph`,
`update-project-state-graph`) and the read-only dashboard. Dates are the
merge dates of the phase PRs; FL-nnn refers to `docs/FUTURE-LOG.md`.

## Unreleased

### fix(ask): the model path
- **The summarize prompt is found under the installed package name** (FL-067, again). `ask/summarize.py` asked `importlib.resources` for `orchestrator.testing`; the wheel installs the backend as `provledger`, so every installed copy raised `ModuleNotFoundError` **inside** the `try` around the model call, and `provledger ask --runner claude` printed `summary unavailable: no model` at a model that was right there. The repo suite could not see it (pytest puts `orchestrator-backend` on the path) — `scripts/pkg_smoke_test.py` now asserts the prompt loads from the wheel.
- **One note per cause.** `summary unavailable: no model` was printed for four different things. Now: `no model configured` · `no candidate nodes matched the question` · `model returned nothing (rc 3; stderr: …)` · `model call failed: <exception>` · `model call timed out after N s`, with `degraded_reason` on the doc and on the page (`data-degraded-reason`).
- **`ask_log.runner_detail`** (migration 029, append-only): per model call — outcome, the note the reader got, the command, rc, the head of stderr, wall time, prompt/answer sizes and the head of the raw answer.
- **`ask.runner`**: a runner may return `text` or `(text, detail)`; `claude_arbiter.default_runner` returns `(text, detail)` and raises `RunnerTimeout` / `RunnerError` instead of swallowing every failure into `''`; `text_runner` is the thin shim the arbiter keeps using.
- **A refusal is repeated, not discarded.** When the account's model budget ran out, `claude -p` exited 1 and printed good JSON with `is_error` and `result` = "You've reached your Fable limit. Switch to another model, or manage usage credits at …". The runner judged the exit code first and the note said `model returned nothing (rc 1; non-zero exit)`, throwing away the only useful sentence in the run. The JSON is now read before the exit code; a refusal is its own outcome and the note is `model call refused [fable]: <the model's own sentence> — try --model sonnet (no model is substituted automatically)`.
- **`PROVLEDGER_ASK_MODEL`** names the model (`--model` wins), resolved once in `ask.run` so both model calls, the note and `ask_log.model` agree. Nothing retries with a different model: an answer whose model was swapped in silently is an answer whose provenance is a guess.
- **Headless calls stop inheriting the host's environment.** `claude -p` reads the user's `~/.claude/settings.json`; on the machine this was found on it says `"language": "Chinese"`, so every headless call (ask, `ClaudeArbiter`, `significance`, the external trigger) answered in Chinese against English prompts — and asking for English *in the prompt* does not override it. `claude_command()` now passes `--settings` pointing at a file of ours (`{"language": "en"}`, one temp file per process, removed at exit; `$PROVLEDGER_CLAUDE_SETTINGS` overrides).
- **The summary is a JSON object, so plugin noise falls off.** The host's plugins write into `result`: claude-mem prepends "Memory capture is currently paused due to a quota cooldown…" to every answer, and `enabledPlugins: {}` in our settings does not stop it. Parsed as prose it counted as *a sentence the model invented with no citation*. `prompts/ask.md` now demands `{"sentences": [...]}` and `summarize.parse_sentences` takes that object out of the reply; a reply that is not that shape is its own degraded reason (`not_json`) with the raw head kept.
- **The answer is in the language you asked in, and that is reported, never enforced by deletion.** `--lang` / `?lang=` switched the scope line only, so a model answering in Chinese against an English prompt went through unremarked; `prompts/ask.md` now asks for English right after the answer shape, with an English example. Deleting a CJK sentence from an `en` answer produced `(nothing survived the checks)` over a paragraph whose every citation was right. Citations and numbers are correctness and still delete; language is a preference, so `dropped["language"]` is gone and `language_mismatch` (`None` | `some` | `all`) travels with the answer, with a note naming the fix.
- **CLI**: `--runner` defaults to `$PROVLEDGER_ASK_RUNNER` (the dashboard has read it since phase 2e and the CLI ignored it), accepts `none`, and `--timeout S` (default 180) bounds each model call.

## 0.2.0 — 2026-09-15

The dual-layer node model, end to end: a project's code is observed as
nodes with stable identities across runs, every plan closes with reasons and
expectations, outcomes are measured later by channels, and an arbiter may
link identities only after clearing a numeric gate. 1048 tests across seven suites.

### Phase 8 — calibration by construction, a real arbiter through the gate, two dashboard pages
- **Calibration set generated by construction** (FL-041): corpus swap variants declare their truth in `expect.toml`, negatives (2:2 "none"), partial links (1:2) and dataflow-layer variants are built programmatically; `analyzer calibration generate|stats`; 48 items (pairs 16 / none 16 / partial 16) plus the person-labelled live export.
- **`ClaudeArbiter`** (FL-040): a headless-`claude` arbiter (`claude -p --output-format json --tools ""`, the user's own login, no API key) with a strict JSON answer contract; offline tests inject a runner; it runs by hand through `arbiter-eval` and is wired only if the gate passes. Run once on the 67-item set (×3): consistency 0.701 / coverage 0.433 / accuracy 0.552 — **refused** (consistency < 1.0), not wired; it never produced a wrong pair.
- **`/outcomes`** (FL-042): every claim across plans with its latest outcome, tier labels, delta / signal, who stated and who backfilled it; `?project=` filter.
- **`/node/{project}/{qualified_name}`** (FL-009): a node's upstream / downstream (card), its history grouped by run (with plan links and tier labels, `identity_asserted` shown with its evidence), its close-time reasons and anchored constraints (a restricted constraint shows only `why_ref`) — one read-only query; the plan page's Reasons panel links to it.
- `Row.context`, `psg_bridge.card_of / latest_qualified_name`, `docs/arbitration.md` §2b/§6, `CHANGELOG.md`.

### Phase 7 — outcome channels, arbitration interface, history backfill (PR #43, 2026-09-14)
- **Outcome channels** (FL-007): `metrics` table + `record-metric` / `metrics_from_stdout`, `OutcomeChannel` protocol, `ProfileDriftChannel`, `MetricChannel` (nearest-before / after, delta_pct), third-party channels via `provledger-extensions.json`; the phantom-uplift demo's +23% becomes an observed outcome of a "±5%" claim; 🎯 Outcomes panel on the plan page.
- **Arbitration interface + gate** (FL-027 part): `graph_api.Arbiter`, `HeuristicArbiter`, `provledger.testing.calibration` (run / report / gate: consistency 1.0, accuracy ≥ 0.9 on ≥ 10 labelled, sha match), `analyzer ambiguities --export`, `analyzer arbiter-eval`, gated wiring in `cli.run`, `selfcheck arbiter_gate`.
- **History backfill** (FL-004): `analyzer backfill <repo> --since [--until] [--every] [--subdir] [--fresh-db]` replays past commits into a graph through worktrees, `trigger=backfill`, resumable.
- FL-034 handled at the review gate (`re_exported_symbols` → warning); `run_provider(isolate="subprocess")`; `docs/outcomes.md`, `docs/arbitration.md`.

### Phase 6 — the host API in the package, conformance, built-ins as reference providers (PR #42, 2026-09-14)
- `provledger.graph_api` (Signature / NodeObservation / ExtractionContext / NodeTypeProvider / the host matcher `match`) — the analyzer's only backend import is `analyzer._host` (FL-006).
- `provledger.testing.conformance.run`: six contracts every node-type provider must keep (determinism, purity, stability matches declaration, schema, failure isolation, performance budget); a liar provider is caught; `docs/conformance.md`.
- `provledger.providers` (run_provider with isolation and degradation records, `builtin_symbols` / `builtin_owned` as reference providers), `extensions_json.providers`, `selfcheck providers_degraded`.
- FL-029 (bare-name gates → qualified resolution), FL-030 (`review_run --as-recovery`, `--accept-stale`, reopened close checks the registry sha).

### Phase 5 — declarative registration (PR #41, 2026-09-14)
- `provledger-extensions.json`: constraints, analyzer name sets, drift kinds, providers and outcome channels registered without source changes; discovery, priorities, fingerprint on every run (`analysis_run.extensions_json`); `ledger_cli import`; `docs/extensions.md`.
- FL-028 (aborted runs marked, never a predecessor), survival re-judged per close, `review_run` dirty-tree guard.

### Phase 4 — scenario suite and the deterministic review driver (PR #40, 2026-09-14)
- `skills/project-state-graph/scripts/tests/scenarios/`: a synthetic project, nine timeline scenarios asserted as event streams with `must_not`, one golden per scenario, fully isolated.
- `review_run.py`: the update-project-state-graph review as one deterministic, injectable driver (gates → refresh → reason slots → fill → close); FL-023 / FL-024 fixes; migration self-heal; `test_llm_consistency.py` (deselected by default).

### Phase 3.5 — explicit project attribution (PR #39, 2026-09-13)
- `Plans.project` / `project_source` decided at publish (declared, `"none"`, or the repo published from) — FL-014; migrations run on every open (FL-021); re-open after a regular-step recovery (FL-022); slimmer `impact_context`.

### Phase 3 — reasons, constraints, tiers (PR #38, 2026-09-11)
- Close-time **reason slots** (a closed set: the nodes the plan changed), `unstated` backstopped by the system, never invented; **anchored constraints** with `why_visibility` (restricted rationale never leaves the ledger); tier labels (`observed` / `derived` / `asserted` / `stated` / `unstated`) on the dashboard, text first, colour second.

### Phase 2 — node history (PR #37, 2026-09-10)
- `node_snapshot` / `node_event` (append-only), three-layer identity (qualified name, structural signature, dataflow signature) plus owner inheritance; `identity_ambiguous` is recorded, never silently linked; `analyzer history <node>`.

### Phase 1 — the refactor-mutation corpus (PR #35, PR #36, 2026-09-10)
- `provledger.testing` corpus (cases × mutations) and the identity-stability harness; phase-1 dogfood fixes FL-011 / 012 / 013 / 015; `python3` everywhere.

## 0.1.0 — 2026-07-24
- First pip-installable `provledger`: the orchestrator backend (plans, steps, deviations, circuit breakers, migrations), the four skills, the read-only dashboard, the project-state-graph analyzer, the phantom-uplift and silent-class-drop demos.
