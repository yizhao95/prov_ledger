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

1. **Two commands, and no others.** This skill runs `provledger ask …` and
   `provledger ask submit …`. Nothing else: no edits, no writes, no other
   tools, no reading source files to "check" an answer. If the ledger does not
   say it, the answer does not say it.
2. **Never answer from memory or from the repository.** You may have read this
   code earlier in the session. That is not a recorded decision, and mixing it
   in is exactly the failure this tool exists to prevent.
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

### c · Submit it for checking (code checks and records)

Write the draft to a temporary file, then:

```bash
provledger ask submit <ask_id> --answer-file <file>
```

The code reads it back sentence by sentence and deletes: a sentence with no id;
a sentence citing an id the table does not hold; a sentence containing a number
the table does not state (the whole sentence, not the number); anything past the
eighth. Every deletion is counted and printed, and the surviving answer is
appended to the ledger as the next version — never an overwrite.

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
