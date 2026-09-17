# Known issues and limits

What provLedger does not do yet, and where it will surprise you. Everything
here is confirmed on a real run, not a guess. Each item says what happens
today, why, and how to work around it when there is a way.

This is the user-facing half of the project's deferred-work ledger; the full
ledger, including everything already finished, is kept out of the repository.

---

## Model calls

**No model arbiter is wired in, so identity is decided deterministically.**
When a symbol moves or is renamed, provLedger matches it by qualified name,
structure signature, dataflow signature and owner. A model arbiter exists
(`ClaudeArbiter`) but has never passed the consistency gate on the calibration
set, so it stays off and `identity_asserted` never appears on a real graph.
Ambiguous cases are reported as ambiguous rather than guessed.

**A failed headless model call can look like silence.** The default runner
collapses a timeout, a non-zero exit, an HTTP 429 and a missing `claude`
binary into the same empty answer. The verdict now distinguishes "no answer"
from "unparseable" and the gate rejects a run containing either, but the
underlying *reason* the call failed does not reach the log. If judgements
suddenly all come back empty, check that `claude` runs and that you are not
rate-limited.

**Headless model calls inherit your host environment.** The `language` setting
in `~/.claude/settings.json` decides the reply language and no prompt can
override it, and other installed plugins can prepend their own text to the
result. provLedger works around this with an isolated settings file and strict
JSON output, but there is no self-check that warns you before the call.

**Anchoring a number to a document needs an exact match.** `anchor candidates`
intersects the literal numbers in the file with the numbers in the metrics
table, so `3.2` will not match `3.20`, `3.2%` or `0.032`, and thousands
separators and currency symbols defeat it. Write the value the same way in
both places, or anchor by hand.

**Judging external artifacts is not usable yet.** The external-trigger judge
stays behind a gate that needs ten human labels, but the only way to label one
is `provledger trigger label <id>` after finding the id yourself in
`trigger_log` — there is no pending list on the CLI or the dashboard, so in
practice the gate is never reached. Prompts are also unversioned: editing
`orchestrator/testing/prompts/external_trigger.md` invalidates every earlier
report and label for that judge without saying so, because reports record the
runner's name and not the prompt's hash.

## Analysis and matching

**Notebooks are not analysed.** `.ipynb` files are not parsed, so notebook
cells never become nodes and changes inside them are invisible to the graph,
the reasons and the headline.

**Split and merge history is flattened.** A node whose history forks — one
function split into two, or two merged into one — is recorded as a break with
an annotation, not as a DAG. The pieces get fresh identities.

**Two things weaken matching: unknown dtypes and re-exports.** The dataflow
layer depends on dtype coverage, and when too many types resolve to unknown the
signature is marked trivial and excluded, leaving qualified name and structure
to carry the decision. Separately, the resolver does not follow imports: if a
definition moves and the old module re-exports it, unchanged callers of the old
qualified name are reported as stale references — downgraded to a warning when
the old module still binds the name at module level, but still reported.
`--accept-stale` is the escape hatch.

**Provider extension points stop short of identity.** A provider may declare
its own `x-…` signature layer and the schema accepts it, but `graph_api.match`
consults only the four built-in layers, so the custom layer has no effect on
identity. A provider's `schema_version` is likewise recorded on every snapshot
but never acted on: raising it does not convert attributes already stored, and
matching does not distinguish versions.

## Graph and dashboard

**The full graph view is slow on a large repository.** On this repository
`/graph` in full mode is roughly 2,650 nodes, 12,000 edges and 3.8 MB of HTML,
which vis-network renders slowly and which makes the node table unusable. The
story and focus modes are the ones built for daily use (about 260 KB and
66 KB) — stay in them unless you specifically need the whole picture.

**Historical graph views use today's edges.** Edges have no run dimension, so
viewing an older analysis run maps the current edges onto that run's nodes.
Node membership is historical; the arrows between them are not. The page says
so in a footnote.

**The consistency card is where two limits meet.** Its callers and output
consumers are stored as bare names rather than qualified names, so a node page
can link to a name that resolves to "not in the state graph". And because the
`removed_upstream` rule walks that card's callees, a deleted function — already
gone from the card — never triggers the one warning most worth having. Declared
nodes are the exception: retiring a declaration does surface as
`removed_upstream`, because declarations are append-only.

**A project registered outside the default directory loses its history on the
first review.** The review refresh calls `init_project.sh` with `--name` and
`--repo` but not `--out-dir`, so the graph is rebuilt in
`~/skill-workspace/project-graphs/` instead of the registered path. Every node
then looks newly added and you get a reason slot for each one. Register
projects in the default location until this is fixed.

**Every review refresh rebuilds the whole graph.** Refresh cost tracks the size
of the repository, not the size of your change: on this repository four
instrumented runs took 12.5–13 minutes each with a 122–125 MB peak, whether the
task touched three files or thirty.

## Reasons, ledger and export

**One constraint can appear several times.** A constraint that names more than
one subject is stored as one row per subject, so `why` and the dashboard can
show it repeatedly. `why` and the PreToolUse injection de-duplicate by
statement when they display it, but `read_hit` still records one row per
stored copy, so the "shown" counts are inflated for such constraints.

**Commas truncate `--ref` labels.** `provledger note --ref` splits `key=value`
pairs on commas, so a label containing a comma is silently cut short at the
first one. Avoid commas in reference labels.

**Questions asked in `/ledger` do not count as "shown".** Reading a constraint
through the ledger's question box deliberately records no `read_hit` — a
question is not a plan. The side effect is that a constraint you have only ever
seen there still appears in `provledger why --never-read`.

**Full-text search covers only part of the ledger.** FTS5 indexes the
interpretation and statement of a reason. Utterance text and reference labels
are matched token by token with `LIKE`, which neither scales nor stems, so
searching for "hashing" will not find "hash".

**Extensions come from one file, and removing an entry does not retract it.**
Only the first extensions file found is read, so a team-level and a repo-level
file cannot be merged. `ledger_cli import` only adds and skips: deleting a
constraint from the file leaves the imported entry active. Supersede it
explicitly instead.

**Anchors are only as trustworthy as the local repository.** Plan closes are
anchored into `refs/notes/provledger`, and `provledger verify --against-notes`
compares against them — but anyone who can rewrite the ledger can also
force-push that ref, and `verify` does not distinguish a local note from one
confirmed by a remote. A plan is anchored only when it closes, so a crashed or
failed plan leaves a gap. There is also no way to void a note written in error;
ten empty anchors in this repository's own notes are counted, not removed.

**Export bundles are always complete, and the recipient cannot check them
independently.** `export --out` rewrites the whole directory every time; there
is no incremental "only what changed since last time" bundle. A recipient can
read `manifest.json` but has no standalone verifier to recompute the hashes
without installing this package. The leak scan also matches whole strings and
skips personal text shorter than 12 characters, so it proves no personal record
was exported intact — not that no personal line leaked.

**The context-pack budget is per plan, not per target.** The budget is about
3,000 tokens for the whole pack, but pack size follows the number of declared
targets. A plan that declares eleven targets has been measured at 3,063 tokens
— over budget by 2%. Writing an honest, complete target list is what pushes it
over.

## Packaging and environment

**Run the test suites one at a time.** Passing several suite directories to
pytest in one command produces spurious failures: each suite relies on its own
`conftest.py` to set the import path, and a combined run moves the rootdir to
the common ancestor, which changes when conftest loads and in what order
`sys.path` is extended. Two test file names also collide across suites. A
combined run of five suites gives 34 failures; run separately they are all
green. `INSTALL.md` lists the suites in the order to run them.

**Webapp test imports depend on collection order.** `orchestrator-webapp/tests`
can import the `app` package only because one test file inserts the parent
directory into `sys.path` at collection time. A new test file that sorts before
it will make the whole suite fail to import. Until there is a
`conftest.py`, new files in that directory may need the same insert.

**`PSG_REGISTRY_PATH` does not fully isolate a project.** `init_project.sh`
honours it for the registry, but the human-readable index is still written to
`~/skill-workspace/project-graphs/PROJECT-STATE-GRAPHS.md`, so isolated tests
and demos still touch the host index.

**The PreToolUse hook logs a `NameError`.** `hook-errors.log` (surfaced by
`selfcheck`'s `hook_failures`) shows `NameError: name '_retract_injected_once'
is not defined`. The hook is non-blocking by design, so this degrades the
injection rather than stopping a tool call.

**The session that installs the plugin does not count itself.** Hooks load when
a session starts, so the session in which you install provLedger records no
tool calls and no utterances. Start a new session before reading any
measurement, and run `metrics baseline --write` there.
