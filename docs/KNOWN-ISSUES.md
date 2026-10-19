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

**Every review refresh rebuilds the whole graph.** Refresh cost tracks the size
of the repository, not the size of your change: on this repository (about
21,700 nodes and 82,400 edges) a full refresh takes about 25 seconds, whether
the task touched three files or thirty. Each refresh also appends a snapshot row
and an event for every node, changed or not, and first copies the whole previous
graph to `provledger.<commit>.db` beside it; the newest five copies are kept.

## Reasons, ledger and export

**One constraint can appear several times.** A constraint that names more than
one subject is stored as one row per subject, so `why` and the dashboard can
show it repeatedly. `why` and the PreToolUse injection de-duplicate by
statement when they display it, but `read_hit` still records one row per
stored copy, so the "shown" counts are inflated for such constraints.

**A `--ref` label cannot contain a comma.** `provledger note --ref` splits
`key=value` pairs on commas, so a label with a comma is refused
(`--ref needs key=value pairs …`) and nothing is written. Leave commas out of
reference labels.

**Questions asked in `/ledger` do not count as "shown".** Reading a constraint
through the ledger's question box deliberately records no `read_hit` — a
question is not a plan. The side effect is that a constraint you have only ever
seen there still appears in `provledger why --never-read`.

**A question in Chinese reaches English records only through `--also`.** Han
text is matched by pairs of adjacent characters against what was recorded in
Chinese — the user's words and any Chinese reason or source label. Reasons and
code names recorded in English are reached through the English words passed with
`--also`, which the `/ledger` and `/receipts` skills add. Japanese kana and
Korean are not matched yet.

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

**Handing your ledger to someone else is manual.** There is no command for it.
Copy the ledger as described under "Copy the ledger with SQLite, not `cp`",
together with each project graph `projects.json` names. `projects.json` stores
the repository and the graph by absolute path, and a session finds a file's
project through that path: on a machine where the paths differ, point `repo`
and `db_path` at that machine's paths, or edits there get no pre-edit reminder
and plans are not attributed to the project. The ledger does not record who
typed a prompt, so words typed in the other person's sessions are kept as the
user's own words and cited that way. The copy holds everything in the ledger,
`personal` records included; `export` is what leaves them out. Taking the ledger back the same
way replaces whatever your own copy recorded in the meantime.

## Packaging and environment

**Run the test suites one at a time** — `bash scripts/run_tests.sh` does.
Passing several suite directories to pytest in one command produces spurious
failures: each suite relies on its own `conftest.py` to set the import path,
and a combined run moves the rootdir to the common ancestor, which changes when
conftest loads and in what order `sys.path` is extended. Two test file names
also collide across suites.

**claude.ai and Cowork do not install the plugin.** The plugin puts `provledger` on
the session's PATH through a top-level `bin/` directory, and both refuse a plugin
that has one
([platform support](https://claude.com/docs/plugins/platform-support)). Without
`bin/` it would still record nothing in claude.ai chat, which ignores a plugin's
hooks; Cowork loads hooks, but that path has not been tried. Install it in Claude
Code: the terminal, the IDE extensions or the desktop app's Code tab.

**A session's end can still go unrecorded under heavy concurrent writing.** The
ledger runs in WAL, so reading never holds a write up, and a prompt or a tool
call that cannot get the write lock within two seconds is written to
`orchestrator.db.spool.jsonl` beside the ledger and replayed, with the time it
happened, by the next hook. The Stop hook's row is not spooled: when another
writer holds the lock past two seconds, that session's end is logged to
`hook-errors.log` and not recorded.

**Copy the ledger with SQLite, not `cp`.** In WAL, recent commits can sit in
`orchestrator.db-wal` until SQLite folds them into the main file, so a plain file
copy can miss them. Use `sqlite3 orchestrator.db ".backup copy.db"`, or
`provledger`'s own `db.copy_ledger` from Python. The `-wal` and `-shm` files
beside the ledger are SQLite's and belong to it.

**A session records nothing before the plugin loads into it.** Hooks load when a
session starts, or when `/reload-plugins` runs (closing the `/plugin` panel runs
it). In the session where you install provLedger, the tool calls and words before
that moment are not recorded, and the session is counted from there. Start a new
session before reading any measurement, and run `metrics baseline --write` there.
