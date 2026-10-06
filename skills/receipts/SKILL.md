---
name: receipts
description: "Help the user reply to a colleague who questioned a decision — read-only. Use when the user types `/receipts <what they said>`, or asks how to answer someone who challenged a number, a change or a choice in this project (\"why did you change this\", \"didn't we agree the other way\", \"where did this number come from\"). Writes a courteous reply grounded ONLY in recorded rows, lists the evidence under it with a record id per line, and asks whether the tone needs adjusting. Never sends anything. Changes nothing the project depends on."
---

# Receipts — answer the colleague, on the record

Someone questioned a decision. The user wants to reply well and not carry blame
for something they can account for. Your job is to hand them **a reply they can
paste**, with the record under it.

`/ledger` is for the user's own questions about the project's history. This is
the other one: the user is not asking what happened, they already know — they
need it said to somebody else, in a tone that holds up, resting on rows.

**You are the model.** No second one is spawned: the code searches the ledger
and computes what is missing, and *you* decide which records the challenge is
actually about and write the reply. The code's ranking is word overlap, and word
overlap can put first a node whose name shares words with the question but has
nothing to do with it. So here the score only orders the candidates, and the
flow below has two reads with your judgement in between. (`provledger ask`,
behind `/ledger`, still takes its top-scored nodes when no model is called;
`/receipts` does not.)

## The hard rules

1. **The `provledger` reads below, and nothing else.** Every read this skill
   documents is permitted, and several are required — `receipts candidates`,
   `receipts facts`, `graph`, `why`, `record`, `plan`. Forbidden is everything
   that is not one of those: no edits, no writes of your own, no querying the
   database by hand. If the ledger does not say it, the reply does not say it.
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
3. **Every line of evidence ends in the id it rests on.** The reply itself reads
   like a person wrote it (ids in prose are unreadable), but every claim in it
   must map to a line in section b. If you cannot point at a row, the sentence
   does not go in the reply.
4. **Every number comes off a record, never out of your head** — not a count you
   worked out, not a rounding, not a date you inferred.

   A number the fact table does not happen to state is **not** forbidden: a
   figure read out of a step log is sourced, and the checker names such numbers
   rather than deleting them. So quote the measurement you found and say which
   record or step it came from. What is forbidden is a number no record states.
5. **An absence is a usable reply.** "There is no record of why that was chosen"
   is often the honest answer, and said plainly it is *still* a good reply — it
   names what is known, names what is not, and offers to find out. Reproduce the
   computed absence sentences verbatim in section b; never write one of your own,
   and never let a gap in the record turn into a plausible-sounding explanation
   in the reply.
6. **You never send it.** You produce text. Whether it goes out, to whom, and in
   what tone is the user's decision, and section c is where you hand that back.
7. **Say only what a record says it is.** A source is a pointer: the ledger holds
   its label, its time and its link — never its body. Describe it the way the
   record does ("an email, linked, never checked"); never write that the email is
   attached, that you have it, or offer to send it. If the colleague wants the
   original, tell the user where the pointer leads so they can find it. A tier is
   part of what a record says, too: `asserted` is a reading recorded as such — the
   agent's or a person's (`recorded_by` says which) — not a quote and not something
   anyone agreed; only a `stated` record carries someone's own words.

## What the ledger holds

You decide for yourself which history to read. To decide well, know what is in
there and how it connects — everything below is queryable, and none of it is
inferred:

**Nodes** are the things a decision can be about: a function, a column, a table,
or something declared by hand that lives outside the code (a business rule, an
external system, a stakeholder decision). They sit in a graph, so each one has
**upstream** and **downstream** neighbours — callers, output consumers, data
lineage. A challenge about a number is often really about something upstream of
it.

**Records hang off nodes**, and the kind matters because each kind answers a
different challenge:

| kind | what it answers |
|---|---|
| a **reason** | why this, and who said so. Carries a **tier** (`stated` = the user's own recorded words / `asserted` = someone's reading of them / `derived` / `unstated` = nobody knows) and separately an **evidence level** (`linked` to a source you can open / `verbal` / `task_context` / `unstated`) |
| a **constraint** | something that must hold. An *active* one is still binding |
| a **rejected** path | an option considered and turned down — the answer to "why didn't you just do it the other way", which is never in the current code, because code only remembers the winner |
| a prior claim / **expectation** and its outcome | something believed at the time, and what was actually observed. An expectation with a failed outcome is a recorded mistake |
| an **anti-pattern** entry | tried it, it failed, here is why. Being read again is the only reason that row exists |
| a **reference** | a pointer to a source outside the ledger — an email, a meeting, a ticket — with when it was last opened and whether it still opens |
| an **influence** row | a task that was shown a record and actually changed because of it |
| a **measured value** | a number someone observed, with its unit and when |

**History is append-only and dated**, so order is recoverable — which is what
answers "wasn't it agreed the other way round". Nothing is ever overwritten.

**Tasks (plans) have steps, and steps have revisions**, with the logs of what
actually ran. One thing to know about them: a task whose failure was later
recovered **closes as COMPLETED**, so a detour disappears from the task's own
status. The ledger is the only place that still remembers it happened.

## How to read it

**You decide what to open.** Nothing below pre-selects for you; these are ways to
look, not a route to follow. Reading is a loop — open something, read it, follow
what it points at, open the next — and every read tells you the command that goes
deeper.

### Start from the code

The quickest way in is usually the repository, used as an **index**:

```bash
grep -rn "<the constant, function or column they asked about>" --include=*.py .
provledger why <path/to/file.py>:<line> --project <name>
```

`why` accepts a `file:line` and resolves it to the node the ledger knows. Be aware
it resolves to the **nearest** node, so a module-level constant can land on a
neighbouring function — check the name it printed is the one you meant, and widen
with `graph` if not.

From there the chain runs on its own, and this is the part worth knowing:

> a node's records → **one record names the plan and step that produced it** → that
> task's steps, their failures and their logs

`provledger record '#<id>'` prints `plan <plan-id> · step <step-id>` for the record
it shows. Take that plan id to `provledger plan` and you have the task: what it was
for, what failed inside it, and what was measured and decided. That last hop is
where the substance usually is, and nothing else will lead you to it.

### The map

```bash
provledger graph [<area|node|nk_…>] --project <name> [--depth N] [--limit N] [--type T]
```

With no target: the graph folded to areas, with how many nodes each holds and
**how many of those carry ledger records** — an area with no records is one you can
skip. With a target: unfold it, and neighbour **names** come back with the edge type
(`calls`, `consumes`, `lineage`, …), each one fully qualified and printed with the
command that opens it — so a name from `graph` goes straight into the next read.

`why --impact` is **not** the same thing: it gives you the counts and **short**
names (`caller · _close`), which `facts` will not take. When you want to walk to a
neighbour, walk with `graph`; use `--impact` to find out whether walking is worth it.

It is folded because it has to be — this project's node names alone run to hundreds
of thousands of tokens. **The folding is pagination, not a judgement about what
matters.** Unfold as wide and as deep as you want; every cut states its size and the
command that lifts it.

### A node's history

```bash
provledger why <node|nk_…|file:line> --project <name> [--all] [--impact] [--search TEXT]
```

A header of counts first — `history`, `constraints (N active)`, `rejected`,
`pending` — so you can tell whether a node is worth opening before you spend
anything on it. `--all` expands the records, `--impact` names the neighbours,
`--search` looks inside record text.

**`why` bounds each record's text.** When it does, it says so and names the read
that has the rest. Do not quote a bounded record as though you had all of it.

### One record, whole

```bash
provledger record '#12'        # or '#r3' for a source
```

The full untruncated text, its tier and evidence level, its dates, who recorded it,
the node it hangs on, the words it quotes, and its sources. **Use this before you
put any record in front of a colleague** — a severed clause can read as though it
says the opposite of what it says.

### A task's steps — and the failures its status hides

```bash
provledger plan <plan_id> [--step <step_id>] [--full]
```

This is the one that catches what nothing else will. **A task whose failure was
recovered closes `COMPLETED`.** The detour is then gone from the task's own status,
and only these step rows remember it: the failed step, its failure reason, its log,
and the nested sub-step that recovered it.

It is not a rare case, and the step **log** is usually where the substance is —
the failure reason says what broke, the log says what was measured and decided
about it. A plan's default view bounds every log, so when a step looks relevant,
open it with `--step <id> --full` rather than reading the stub.

So when a challenge is about *how* something came to be — why a number changed, why
an approach was abandoned — look at the task, and do not read `COMPLETED` as "it
went smoothly".

### Where to start

```bash
provledger receipts candidates "<what the colleague said, verbatim>" --project <name> [--cap N]
```

An **entry point**, nothing more: nodes whose text, names or literals match, each
with `why` (which matcher found it) and `score`, plus the cap and how many were cut.
It states outright that the score orders the list and does not choose.

**Read `why`, not `score`.** A node that matched a common word in its *name*
("review", "run", "test") is usually noise; one that matched a **literal** the
colleague wrote — an identifier, a number, a file name — is usually the subject.
Take a starting point and navigate; and raise `--cap` rather than working from a
truncated list.

### When you know which nodes matter

```bash
provledger receipts facts <qn> [<qn> …] --project <name>
```

The fact table, the timeline oldest-first, the computed absences and the scope line
for the nodes you chose. The timeline order is what answers "wasn't it the other way
round". Graph bookkeeping is left out and counted separately — it records what
changed, never why.

The cite namespace, shared with `/ledger`:

| token | what it is |
|---|---|
| `[#12]` | a ledger record — a constraint, a reason, a rejected path |
| `[#r3]` | a source (email, meeting, ticket, doc) |
| `[#i4]` | an influence row — a task that changed because of a record |
| `[#x6]` / `[#o7]` | an expectation / its outcome |
| `[#m8]` | a measured value |
| `[scope]` | an absence sentence, reproduced verbatim |

A `[#r…]` is the strongest thing to put in front of a colleague: a source outside
this tool, which they can open themselves. Lead with those when they exist.

## Before you claim nothing was recorded

**"I searched and found nothing" does not mean "there is no record."** They are not
the same claim: the first is about where you looked, the second is about the ledger,
and only the first one is ever yours to make.

This is not a hypothetical caution; it has happened repeatedly on this project. A
capable model searched honestly, found nothing, and reported that nothing was
recorded — while the answer sat in a column it had not thought to search. No search
was careless. Each conclusion simply followed from its own coverage, and coverage is
invisible from the inside.

So before writing that something is unrecorded:

- did you look at the **task** as well as the node (`provledger plan`), including a
  task whose status is `COMPLETED`?
- did you read the records **whole** (`provledger record`), or only what `why`
  printed?
- did you try the neighbours (`--impact`), or only the nodes the question named?

If you have not, say what you searched, not what exists. An honest "I looked at
these places and did not find it" is a good reply; "there is no record of that" is a
claim about the ledger, and you can only make it about ground you actually covered.

## What you produce

Three sections, in this order, and the order is the point: the user opened this
to reply to someone, not to read a dossier. Give them the finished thing first.

### a · The reply

The paragraph itself — ready to **copy and paste**, nothing to fill in. Address
what was actually asked. Match the colleague's register: a neutral question gets
a neutral answer; a pointed one gets a calm, specific one, and specificity is
what does the work. Do not perform politeness, do not apologise for a decision
the record supports, and do not volunteer blame the record does not assign.

No ids in this paragraph. No hedging about what you looked at. Just the reply.

### b · The evidence

Under a short heading, one line per claim in the reply, each ending in its id,
oldest first where the order matters. Then the computed absences verbatim, then
the scope line. This is what the user skims before sending, and what they forward
if the colleague pushes back.

If the record does not support part of the reply, that part should not be in
section a — fix section a, do not annotate section b.

### c · The tone

Close by asking whether the tone needs adjusting, and stop. One line. The reply
is written; how it should land is not yours to decide, and this is the only
decision the user actually has to make here.

Then stop. Do not send it, do not offer to send it, do not draft a follow-up.

## What this skill never does

- It **never sends** the reply, or any message, anywhere.
- It never edits, creates or deletes anything in the repository, and it changes
  nothing the project depends on. `receipts candidates`, `receipts facts`,
  `graph`, `record` and `plan` write nothing at all, not even a record of having
  been asked; `why` appends a `read_hit` row for each record it shows, an access
  log and nothing more.
- It never runs tests, builds, or any command other than the `provledger` reads
  above.
- It never fills a gap in the ledger with a plausible explanation. "There is no
  record of that" is a complete and useful answer, and it is the one answer a
  model is worst at giving unprompted — which is why the absence sentences are
  computed and handed to you rather than asked of you.
