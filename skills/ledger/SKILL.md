---
name: ledger
description: "Ask the ledger why / whether we tried — read-only. Use when the user types `/ledger <question>` or asks why something in this project is the way it is, why a value was chosen, who decided it, whether an alternative was ever tried, or whether a change was ever verified. Answers ONLY from recorded rows: every sentence cites the record id it rests on, absences are computed by code, and the searched range is stated. Changes nothing the project depends on."
---

# Ledger — ask why, and whether we tried

The user wants to know why something in this project is the way it is, and to
see the evidence for it — quickly, and without having to wonder whether the
answer was made up. The ledger holds what the code cannot: who asked for a
change, what was said and when, what was tried and rejected, what was checked.
Your job is to read it and tell the cause and the effect plainly, in date order,
with every sentence pointing at a record the user can open.

Code does the parts a model gets wrong: it finds the candidate nodes, builds the
fact table, computes what is missing, and afterwards deletes any sentence that
points at nothing. You read, choose, and write.

## The rules, and why

1. **Answer from the ledger's records, read in this session.** The user will
   repeat your answer to other people as fact, so every claim has to come from a
   `provledger` read you ran just now. **Never answer from memory**: having read
   the code earlier in this session is not a recorded decision.
2. **Read the code freely — for what and where, never for why.** Grep it, open
   it, follow it: that is how you find the node to ask about. What the code
   cannot contain is why it was written that way. The one thing that makes this
   tool worse than useless is to read the code and invent a reason for it ("it
   is written this way, so presumably because X") — the real reason is usually
   recorded, and a plausible guess will be believed instead of it.
3. **Every sentence ends in the id it rests on** — `[#12]`, `[#r3]`, `[scope]` —
   and every number in it is printed in a record. That is what lets the user
   check you, and what `ask submit` checks.
4. **Say what a record is, the way the record says it.** The fact table opens
   with a legend: what each tier and cite token means, and how to put it into a
   sentence. Follow it — it is why a person's words are given as theirs, a
   reading is never called an agreement, and a link is never called the email.
5. **An absence is an answer.** "No reason was recorded for X" is complete and
   useful. Code computes those sentences against the searched range; reproduce
   them word for word, and when you have searched without finding, say where you
   searched rather than that nothing exists.

The reads this skill uses are all permitted and write nothing the project
depends on: `ask`, `ask submit`, `graph`, `why`, `record`, `plan`, and the
`ask card` / `ask feedback` follow-ups.

## The flow

### a · Ask the ledger

```bash
provledger ask "<the user's question, verbatim>" --json --no-model \
  [--project <name>]          # omit inside a registered repo; it is inferred from the cwd
```

`--no-model`: you are the model, so no second one is spawned. The JSON gives
`ask_id`, `facts_text` (the fact table, legend first), `absences`, `scope_line`,
`candidates` and `chosen`. With no model, `chosen` is the top few candidates by
word overlap — a starting point, not a judgement. If a chosen node has nothing to
do with the question, or the table does not settle it, go to b.

If the table is empty, say so and stop: an empty table is an answer.

### b · When the table does not settle it, go and look

The repository is the quickest index:

```bash
grep -rn "<the thing they asked about>" --include=*.py .
provledger why <path/to/file.py>:<line> --project <name>
```

`why` resolves a `file:line` to the **nearest** node the ledger knows, so a
module-level constant can land on a neighbouring function — check the name it
printed. From a node, the chain that usually holds the substance is: the node's
records → **one record names the plan and step that produced it**
(`provledger record '#<id>'` prints `plan <plan-id> · step <step-id>`) → that
task's steps, their failures and their logs (`provledger plan`).

```bash
provledger graph [<area|node>] --project <name> [--depth N]   # the graph, folded; unfold what you want
provledger why <node> --project <name> [--all] [--impact]     # one node's history; --impact names neighbours
provledger record '#12'                                       # one record, whole and untruncated
provledger plan <plan_id> [--step <id>] [--full]              # a task's steps, their failures and logs
```

Two things these reads hide from a quick look:

- **`why` bounds each record's text** and says so. Open the record whole with
  `provledger record` before you rest a sentence on it; a severed clause can read
  as its opposite.
- **A task whose failure was recovered closes `COMPLETED`.** Only its step rows
  still show the failed step, its reason and its log, and the log is usually where
  what was measured and decided is written. Open a relevant step with
  `--step <id> --full`, and never read `COMPLETED` as "it went smoothly".

Whatever you find this way is cited the same way and goes through the same check.

**Searching and finding nothing does not mean there is no record.** The first is
about where you looked; only that one is yours to say. Before you add an absence
of your own, you have looked at the task (`provledger plan`), read the records
whole (`provledger record`) and tried the neighbours (`--impact`); otherwise say
what you searched.

### c · Write the answer

Use this shape. It hands the next person the whole situation — what the thing
is, who asked for it and in what words, what was done, and what to keep in mind
now — in the order it happened, which is the fastest way to see cause and effect.

```
<What it is, or what changed and why, in one sentence — [#id].
 When nothing says why: "No reason was recorded for X. [scope]">

<Where it came from: who asked, when, in their own words — "…" [#id].>

<What was done about it, oldest first: on <date>, which task built or changed
 what, and how; what was tried and rejected on the way — [#id] each.>

<What to keep in mind now: its state as recorded, with dates, and the computed
 absences, word for word.>
```

Leave out a part the records do not cover rather than filling it.
At most 8 sentences. No headings of your own, no list of record links, no account
of how you searched — the command prints the links, and the scope line says what
was searched.

Before you submit, check each sentence once:

- Which read in this session printed what it says? (None → it goes.)
- Does its verb match the record's tier — *said* for `stated`, *recorded as the
  understanding* for `asserted`, *nobody said why* for `unstated`?
- Is every date and number in it printed in a record?

### d · Submit it for checking

Write the draft to a file with the Write tool, then:

```bash
provledger ask submit <ask_id> --answer-file <file>
```

It reads the draft back and deletes a sentence with no id, a sentence citing an
id the table does not hold, and anything past the eighth. A number the table does
not state is named in a note rather than deleted: a number from a step log is
sourced but not citable here, so keep it, cite the record it came from, and let
the note say so.

If a sentence you believe is right was deleted, find the record that supports it
with the reads in b and submit once more with that id. If it is deleted again,
leave it out.

### e · Show the user what it printed

The answer the user sees is the one `ask submit` printed: the answer, the
absences, the scope line and the record links, as they stand. If sentences were
deleted, say how many and why in one line — and do not show them again in other
words: they point at nothing the user can open, so showing them puts back the
very claim the check removed. Close with the two follow-ups:

```
[Open records] provledger why <node>
[Export]       provledger ask card <ask_id> --out card.md
```

If the user says the answer is wrong, record it: `provledger ask feedback <ask_id> wrong`.

## Examples

<example>
Question: what is `reports.returns_snapshot`, and why does it exist?

`reports.returns_snapshot` is a one-off table built on 2026-04-07 for a single request, not a scheduled pipeline [#31].
Priya from returns QA asked for it on 2026-04-02, in her words: "give me every order returned within a week of a price change, so I can follow how those items do" [#28].
The task that built it joined the returns log with the price-change history and ran once by hand [#31].
Refreshing it on a schedule was considered on 2026-04-06 and left out, because the request was for a single cut [#33].
`reports.returns_snapshot` has not changed since 2026-04-07. [scope]

Why this is right: the first sentence says what it is; then who asked, when and in her own words; then what was done and what was left out, with dates; then its state now. The next person gets the whole situation without having to ask.
</example>

<example>
Question: why was `ship.cost.parse_weight` changed?

No reason was recorded for `ship.cost.parse_weight`. [scope]
It was changed on 2026-05-02 by a task whose goal was "accept weights in grams" [#e31], and that task recorded no reason for `parse_weight` itself [#40].
`ship.cost.parse_weight` has never been verified: no outcome is recorded for it in scope. [scope]

Why this is right: the node's own slot says nobody gave a reason, so the answer says exactly that; the task's goal is named as the task's, with its date, not passed off as the node's reason.
</example>

<example>
Question: was caching the rate table ever tried for `ship.cost.lookup`?

Yes: caching the rate table was tried on 2026-06-18 and rejected, because the cached rates went stale within a day [#52].
The task's step log measured the cache as "stale after 26 h" before the step was failed and replaced by a direct lookup [#52].
No alternative to the direct lookup was tested after that: no rejected path and no second measured value in scope. [scope]

Why this is right: it answers yes or no first, gives the date and the recorded reason for the rejection, and quotes the measurement from the record instead of rounding it.
</example>

## What this skill changes

Nothing the project depends on. It never edits, creates or deletes anything in
the repository — the one file it can produce is the evidence card, and only when
the user asks for `[Export]`, at the path they choose. The commands keep their
own access log (`ask` the question, `ask submit` the checked answer, `why` a
`read_hit` per record shown, `ask feedback` the verdict), and that is all they
write. It runs no tests, builds or commands other than the `provledger` reads
above.
