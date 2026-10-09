# The `provledger` command

Every read and write the plugin offers outside a session. The table below is
checked against `orchestrator/cli.py` by `scripts/tests/test_cli_doc.py`, so a
command listed here exists.

## The usual way in is your own code

provLedger **supplements** the repository; it does not replace reading it. Code
shows the winner and nothing else — never the option that was rejected, who
asked for it, or what it cost. That remainder is what the ledger holds, so the
normal path starts where you already are:

```console
$ grep -rn "MAX_RETRIES" --include=*.py .                 # the repo is the index
pipeline/load.py:42

$ provledger why pipeline/load.py:42                      # what the ledger holds on that line
…load._fetch · history 2 · constraints 1 · rejected 1 · pending 0
 #318 · the vendor API rate-limits at five calls a minute; three retries with backoff stays inside it…

$ provledger record '#318'                                # one record, whole
   plan <plan-id> · step <step-id>

$ provledger plan <plan-id>                               # and the task behind it
<plan-id> · COMPLETED · steps 12 · 1 failed · deviations 2
```

*Illustrative: the names, ids and text above are invented to show the shape of each read.*

`why` takes a `file:line` and resolves it to the node the ledger knows. A record
names the task that produced it, and the task's step logs hold what was measured
and decided — which is usually where the substance is. **A task whose failure
was recovered closes `COMPLETED`**, so the detour is gone from its own status and
only those rows remember it.

The one thing to never do is read the code and invent a reason for it. "It's
written this way, so presumably because X" is the failure this exists to
prevent: the real reason is usually recorded, and a plausible guess gets
believed instead of it.

## Commands

`provledger <command>`, one row per subcommand; `--help` on any of them has the rest.

| command | what it does |
|---|---|
| `metrics plan` / `metrics baseline` | tool-call cost of one plan; median / p90 over completed plans |
| `note` | record something that was said, with the time it happened (`--node`, `--ref`, `--kind`) |
| `node declare` | turn one sentence into a declared node — a draft, until you confirm it in your own words |
| `node add` | a figure with no traceable data source (`--manual-figure`, `--value`, `--note`) |
| `node list` / `node show` / `node retire` | the project's declared nodes; every version of one; retire one (append-only) |
| `anchor` | pin a number in a deck, workbook or report to the node it is a reading of |
| `anchor check` | re-read the files; a lost anchor is reported, never re-pointed |
| `anchor candidates` | propose readings — off by default, and even on it only proposes |
| `headline show` / `respond` / `ack` | the plan headline; answer one finding (revise / proceed); a person proceeds past one |
| `why` | one bounded read of a node: history, constraints, rejected paths, prior claims, blast radius (`--impact`, `--all`, `--pending`, `--never-read`, `--search`, `--json`) |
| `graph` | the project graph, folded to areas with how many nodes carry records; a target plus `--depth N` unfolds it and prints neighbour **names** with their edge type (`--limit`, `--type`, `--include-imports`, `--json`). The fold is pagination, never a judgement about relevance |
| `record` | one record whole and untruncated — `#12` for a ledger record, `#r3` for a source — with its tier, dates, the words it quotes, its sources, and the task that produced it (`--json`) |
| `plan` | a task's steps in tree order with their failures and logs (`--step`, `--full`, `--log-chars`, `--json`). The only read that reaches a failure inside a plan whose status is `COMPLETED` because the failure was recovered |
| `ask` | ask the ledger a question (`--no-model`, `--json`, `--export`, `--lang`, `--runner`) |
| `receipts` | someone challenged a decision. `receipts candidates "<what they said>"` is an entry point — matching nodes with how each matched, and it says outright that the score orders the list and does not choose; `receipts facts <node>…` is the timeline, the gaps and the range for the nodes you picked (`--cap`, `--json`, `--lang`). `/receipts` wraps both and writes the reply |
| `verify` | walk the hash chains (`utterance`, `reference`, `change_reason`, `reference_check`), and with `--against-notes` the git anchors they must agree with (exit 3 on a broken chain) |
| `reference add` | pin a decision to where it came from — an email, a meeting, a ticket — as a label and a link, never a copy of the body (`--reason` or `--utterance`, `--uri`, `--stance`) |
| `reference pending` / `mark` | which pointers nobody has opened lately; record that one still opens, or that it stopped (`--ok`, `--gone`, `--moved`, `--no-access`) |
| `review evidence-slots` | what still has no checkable source: node, what changed, this plan's own time window, the identifiers to search on, significance. provLedger hands the list over and searches nothing itself (`--plan`, `--json`) |
| `review evidence-log` | what became of each slot — attached, searched and found nothing, timed out, never searched — so a blank can say which kind of blank it is (`--plan`, `--node`, `--outcome`, `--tool-hint`, `--elapsed-ms`) |
| `export` | a whitelisted bundle for someone who was not there (`--out`, `--zip`, `--include-rationale`, `--md`) |
| `init --agents-md` | write or refresh the provledger section of `./AGENTS.md` |
| `reason mark` | a person's word on a reason's significance, logged as judged by a human |
| `significance eval` / `disagreements` | manual: an LLM verdict on reasons that carry only a hint; where hint and verdict disagree |
| `reasons reclass-status` / `ask-basis` | migration state and tier counts; the close-time questions the rules did not recognise |
| `reasons recheck` | rows the close-time rules wrote before 0.4.6 that are not true: a `stated` reason quoting text Claude Code injected (a subagent's report, a reminder), an R6 rejected path on a node its text does not name. A dry run lists them; `--apply` appends a correction that supersedes each, in one transaction |
| `trigger eval` / `label` | the external-artifact judge: manual replay of its paired examples against a runner, with its gate; a person marks one verdict right or wrong (appended, never overwritten) |

`export` never lets verbatim words out: a shareable record that quotes personal
words keeps the record, drops the quotation and says which one it withheld; a
rationale travels only when you name its id.

`ask`, `why` and `receipts` change nothing the project depends on, but they do
append access-log rows (`ask_log`, `read_hit`) so the ledger can say what was read.
