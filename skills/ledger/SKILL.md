---
name: ledger
description: "Ask the ledger why / whether we tried — read-only. Use when the user types `/ledger <question>` or asks why something in this project is the way it is, why a value was chosen, who decided it, whether an alternative was ever tried, or whether a change was ever verified. Answers ONLY from recorded rows: every sentence cites the record id it rests on, absences are computed by code, and the searched range is stated. Never changes anything."
---

# Ledger — ask why, and whether we tried

The user asks in words. You answer **only** from what the ledger recorded, with
a record id on every sentence, and you say plainly what is not there.

You are not the one who decides which nodes are relevant, what the facts are, or
whether something is missing — code does all three. Your one job is to turn a
computed fact table into a paragraph a person can read, and then hand that
paragraph back to the code so it can delete anything you could not support.

## The hard rules

1. **The `provledger` reads this skill documents, and nothing outside them.**
   `ask` and `ask submit` carry the answer; `graph`, `why`, `record` and `plan`
   (§b2) are there for when the fact table does not settle it. Forbidden is
   everything else: no edits, no writes, no querying the database by hand. If
   the ledger does not say it, the answer does not say it.
2. **Read the code freely. This tool supplements it; it does not replace it.**
   Grep it, open it, follow it — that is your job and nothing here restricts it.
   What the code physically cannot contain is the part this ledger holds: why a
   choice was made, who asked for it, what was rejected on the way, and what it
   cost. The code only ever shows the winner.

   **The one thing to never do is read the code and invent a reason for it.**
   "It's written this way, so presumably because X" is the single failure this
   tool exists to prevent — because the real reason is usually recorded in the
   task that made the change, and a plausible guess will be believed instead of
   it. Use the code to find out *what* and *where*; use the ledger for *why*,
   and when the ledger has no why, say so.

   **Never answer from memory**, either. You may have read this code earlier in
   this session. That is not a recorded decision.
3. **Every sentence ends with `[#id]` or `[scope]`**, and the id must appear in
   the fact table you were given.
4. **No number that is not printed in the fact table** — not a count you worked
   out, not a rounding, not a date you inferred.
5. **Reproduce the absence sentences verbatim.** Code generated them. Do not
   rewrite, soften or merge them, and never write an absence of your own.
6. **At most 8 sentences.** If the table does not answer the question, say so in
   one sentence citing what it does cover.

## The flow

### a · Ask the ledger (code locates and computes)

```bash
provledger ask "<the user's question, verbatim>" --json --no-model \
  [--project <name>]                # omit inside a registered repo; it is inferred from the cwd
```

`--no-model` is deliberate: **you** are the model, so no second one is spawned.
The JSON gives you `ask_id`, `facts_text` (the fact table), `absences`, `scope`,
`scope_line`, `candidates` and `chosen`.

If it returns `"degraded": true` with no nodes, or the fact table is empty, say
so and stop — an empty table is an answer.

### b · Draft the summary (you, under the rules above)

Read `facts_text`. Write at most 8 sentences that answer the user's question,
each ending in the id it rests on. The cite namespace is:

| token | what it is |
|---|---|
| `[#12]` | a ledger record — a constraint, a reason, a rejected path |
| `[#r3]` | a source (email, meeting, ticket, doc) |
| `[#i4]` | an influence row — a plan that changed because of a record |
| `[#e5]` | a change event in the project graph |
| `[#x6]` / `[#o7]` | an expectation / its outcome |
| `[#m8]` | a measured value |
| `[scope]` | an absence sentence, reproduced verbatim |

Prefer the record that *caused* the thing over the records that merely restate
it: an `[#i…]` row is the strongest sentence you can write, because it names a
plan that actually changed.

### b2 · When the table does not answer it, go and look

The quickest way in is usually the repository, used as an index:

```bash
grep -rn "<the thing they asked about>" --include=*.py .
provledger why <path/to/file.py>:<line> --project <name>
```

`why` takes a `file:line` and resolves it to the node the ledger knows — to the
**nearest** node, so a module-level constant can land on a neighbouring function;
check the name it printed is the one you meant.

From there the chain runs itself, and this hop is the one worth knowing: a node's
records → **one record names the plan and step that produced it** → that task's
steps, their failures, their logs. `provledger record '#<id>'` prints
`plan <plan-id> · step <step-id>`; take that plan id to `provledger plan`. The last
hop is where the substance usually is, and nothing else leads to it.

`ask` computes a fact table from the nodes a matcher found. That is a starting
point, not the extent of the ledger. When the table is thin, or the question is
about *how* something came to be, **you decide where to look next** — these are
reads, they change nothing, and each one names the command that goes deeper:

```bash
provledger graph [<area|node>] --project <name> [--depth N]   # the graph, folded; unfold what you want
provledger why <node> --project <name> [--all] [--impact]     # one node's history; --impact names neighbours
provledger record '#12'                                       # one record, whole and untruncated
provledger plan <plan_id> [--step <id>] [--full]              # a task's steps, their failures and logs
```

Two of these exist because of specific ways the ledger hides things from a casual
read:

**`why` bounds each record's text.** It says so when it does. A severed clause can
read as though it says the opposite of what it says, so use `provledger record`
before you rest a sentence on a record you have only seen bounded.

**A task whose failure was recovered closes `COMPLETED`.** The detour then vanishes
from the task's own status, and only the step rows remember: the failed step, its
reason, its log, and the sub-step that recovered it. The **log** is usually where the
substance is — the reason says what broke, the log says what was measured and decided
about it — and the default view bounds it, so open a relevant step with
`--step <id> --full`. Never read `COMPLETED` as "it went smoothly".

Whatever you find this way is cited the same as anything else, and it still goes
through the check in step c.

### b3 · Before you say there is no record

**"I searched and found nothing" does not mean "there is no record."** They are not
the same claim: the first is about where you looked, the second is about the ledger,
and only the first is ever yours to make.

This has happened repeatedly on this project: a capable model searched honestly,
found nothing, and reported that nothing was recorded — while the answer sat in a
column it had not thought to search. No search was careless; each conclusion simply
followed from its own coverage, and coverage is invisible from the inside.

The absence sentences `ask` hands you are different — code computed those against a
stated range, which is why you reproduce them verbatim instead of writing your own.
But before adding any absence of your own, check that you looked at the task
(`provledger plan`), read the records whole (`provledger record`), and tried the
neighbours (`--impact`). If you have not, report what you searched, not what exists.

### c · Submit it for checking (code checks and records)

Write the draft to a temporary file, then:

```bash
provledger ask submit <ask_id> --answer-file <file>
```

The code reads it back sentence by sentence and **deletes** two kinds of sentence:
one with no id, and one citing an id the table does not hold — plus anything past
the eighth. Those are defects the machine can actually see.

A **number the table does not state is named, not deleted.** The sentence stands and
the note lists the number, because the check can tell whether a number is in its own
table but not whether it is true — and a number out of a step log is sourced yet
uncitable here, so deleting on that basis was removing correct answers. **Do not
self-censor a number you read in a record.** Write the sentence, cite the record it
came from, and let the note say the number is not in the table; that is information
for the reader, not a verdict against you.

Every deletion is counted and printed, and the surviving answer is appended to the
ledger as the next version — never an overwrite.

### d · Show the user what it printed

Relay the command's output as it stands: the answer, the absences, the scope
line, the cited records with the URL each one opens, and the two follow-ups:

```
[Open records] provledger why <node>
[Export]       provledger ask card <ask_id> --out card.md
```

If sentences were dropped, **say so in your own reply too** — a trimmed answer
must not be presented as a complete one. Then stop. If the user says the answer
is wrong, the record of that is `provledger ask feedback <ask_id> wrong`.

## What this skill never does

- It never edits, creates or deletes anything in the repository.
- It never runs tests, builds, or any command other than the two above.
- It never fills a gap in the ledger with a plausible explanation. "There is no
  record of that" is a complete and useful answer, and it is the one answer a
  model is worst at giving unprompted — which is why the absence sentences are
  computed and handed to you rather than asked of you.
