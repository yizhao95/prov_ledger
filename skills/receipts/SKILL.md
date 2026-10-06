---
name: receipts
description: "Help the user reply to a colleague who questioned a decision — read-only. Use when the user types `/receipts <what they said>`, or asks how to answer someone who challenged a number, a change or a choice in this project (\"why did you change this\", \"didn't we agree the other way\", \"where did this number come from\"). Writes a reply grounded ONLY in recorded rows, lists the evidence under it with a record id per line, and names what the user should confirm or add before sending. Never sends anything. Changes nothing the project depends on."
---

# Receipts — answer the colleague, on the record

Someone questioned a decision, and the user has to answer them. What helps most
is a reply that hands the colleague the whole situation: what the thing is, who
asked for it and in what words, what was done and when, and what holds now — so
the next person understands without another round of questions. Your job is to
write that reply from the ledger, show the records under it, and tell the user
plainly which parts the record does not settle, so they can confirm or add them
before they send it.

`/ledger` answers the user's own question, strictly. This one speaks to someone
else on the user's behalf, so it aims to be complete — and it is honest about
where completeness runs out.

**You are the model.** No second one is spawned: the code searches the ledger and
computes what is missing; you decide which records the challenge is about and
write the reply. The code's ranking is word overlap, which can put first a node
that only shares a word with the question — so the score orders the candidates
and your judgement picks.

## The hard rules

1. **Answer from the records you read in this session.** The colleague may forward
   the reply as evidence, so every claim in it comes from a `provledger` read you
   ran just now: `receipts candidates` and `receipts facts`, and `graph`, `why`,
   `record` and `plan` to follow a record to the task behind it. All of them are
   permitted, and none changes anything the project depends on.
   **Never answer from memory**: having read the code earlier is not a recorded
   decision.
2. **Read the code freely — for what and where, never for why.** Grep it, open it,
   follow it; that is how you find the node. What the code cannot hold is why it
   was written that way, and the one failure this tool exists to prevent is to
   read the code and invent a reason for it. Use the code for *what* and *where*,
   the ledger for *why*, and when the ledger has no why, say so.
3. **Every line of evidence ends in the id it rests on.** The reply reads like a
   person wrote it, with no ids in it; every claim in it maps to a line in section
   b. A claim you cannot point at does not go in the reply — it goes in section c,
   as something for the user to confirm.
4. **Say what a record is, the way the record says it.** The material opens with a
   legend: what each tier and cite token means and how to put it into words. A
   reason a person stated in their own words is theirs: give it as theirs and
   trust it, with the day they said it. An `asserted` record is a reading recorded at the time,
   the agent's or a person's (`recorded_by` says which) — not a quote and not
   something anyone agreed. A source is a pointer: the ledger holds its label, time
   and link, never its body, so never write that the email is attached, that you
   have it, or offer to send it; if the colleague wants the original, tell the user
   where the pointer leads.
5. **A task's goal is not a node's reason.** When the node the colleague asks about
   has no reason of its own (`unstated`), say the task which changed it recorded no
   reason for it — name the task by what it set out to do and when. That is still a
   complete account; it is also a line for section c.
6. **Every number and identifier is copied from a read** — a commit, a plan id, a
   file:line, a date: something a read printed, not a count you worked out, a
   rounding or a date you inferred. The colleague may go and look it up, and an
   identifier nobody printed sends them after something that does not exist. A
   number from a step log is sourced: quote it and say which record or step it
   came from.
7. **An absence is a usable reply.** "There is no record of why that was chosen" is
   often the honest answer, and said plainly it is still a good one. Reproduce the
   computed absence sentences word for word in section b.
8. **You never send it.** You produce text; whether it goes out, to whom and in what
   tone is the user's decision, and section c hands it back.

## What the ledger holds

Everything below is queryable and none of it is inferred.

**Nodes** are what a decision can be about: a function, a column, a table, or
something declared by hand outside the code (a business rule, an external system,
a stakeholder decision). They sit in a graph, so each has **upstream** and
**downstream** neighbours — callers, consumers, data lineage. A challenge about a
number is often really about something upstream of it.

**Records hang off nodes**, and each kind answers a different challenge:

| kind | what it answers |
|---|---|
| a **reason** | why this, and who said so — with a tier (see the legend) |
| a **constraint** | something that must hold; an *active* one still binds |
| a **rejected** path | "why not the other way" — never in the current code, which only remembers the winner |
| an **expectation** and its outcome | what was believed at the time and what was observed; a failed outcome is a recorded mistake |
| an **anti-pattern** entry | tried it, it failed, here is why |
| a **reference** | a pointer to an email, a meeting, a ticket — with whether it was ever checked |
| an **influence** row | a task that was shown a record and changed because of it |
| a **measured value** | a number someone observed, with its unit and when |

**History is append-only and dated**, so the order is recoverable — which is what
answers "wasn't it agreed the other way round". **Tasks (plans) have steps, and
steps have revisions**, with the logs of what ran. A task whose failure was later
recovered **closes as COMPLETED**, so the detour is gone from its status; only its
step rows remember it.

## How to read it

You decide what to open. Reading is a loop — open something, follow what it points
at, open the next — and every read prints the command that goes deeper.

**Start from the code.** The repository is the quickest index:

```bash
grep -rn "<the constant, function or column they asked about>" --include=*.py .
provledger why <path/to/file.py>:<line> --project <name>
```

`why` turns a `file:line` into the **nearest** node the ledger knows — check the
name it printed. From a node, the chain that usually holds the substance: its
records → **one record names the plan and step that produced it**
(`provledger record '#<id>'` prints `plan <plan-id> · step <step-id>`) → that
task's steps, failures and logs (`provledger plan`).

**Where to start when the code does not point anywhere:**

```bash
provledger receipts candidates "<what the colleague said, verbatim>" --project <name> [--cap N]
```

An entry point: nodes whose text, names or literals match, each with `why` (which
matcher found it) and a `score`. **Read `why`, not `score`**: a match on a common
word in a name is usually noise; a match on a literal the colleague wrote — an
identifier, a number, a file name — is usually the subject. Raise `--cap` rather
than work from a cut list.

**When you know which nodes matter:**

```bash
provledger receipts facts <qn> [<qn> …] --project <name>
```

The legend, the fact table, the timeline oldest first, the computed absences and
the scope line for the nodes you chose.

**To go further:**

```bash
provledger graph [<area|node>] --project <name> [--depth N]   # the map, folded; unfold what you want
provledger why <node> --project <name> [--all] [--impact]     # one node's history; --impact counts neighbours
provledger record '#12'                                       # one record, whole and untruncated
provledger plan <plan_id> [--step <id>] [--full]              # a task's steps, failures and logs
```

- `graph` folds because the full graph is too large to print; the folding is
  pagination, not a judgement. Neighbour names it prints go straight into the next
  read; `why --impact` gives only counts and short names.
- `why` bounds each record's text and says so. Read a record whole with
  `provledger record` before you put it in front of a colleague.
- A task that reads `COMPLETED` can hold failed steps. When the challenge is about
  *how* something came to be, open the task and the relevant step with
  `--step <id> --full`; the step log is where what was measured and decided is
  written.

## Before you claim nothing was recorded

"I searched and found nothing" does not mean "there is no record": the first is
about where you looked, and only that one is yours to say. Before writing that
something is unrecorded, you have looked at the task (`provledger plan`), read the
records whole (`provledger record`) and tried the neighbours (`--impact`). If you
have not, say what you searched — and put the open question in section c.

## What you produce

Three sections, in this order: the user opened this to reply to someone, so the
finished reply comes first.

### a · The reply

Ready to **copy and paste**, nothing to fill in. Tell it as a timeline, in the
order things happened, because dates carry a reply — when it was said, when it changed, when it
was last checked — and a colleague can use a date where a plan id means nothing:

- what the thing is, or what changed;
- where it came from: who asked, when, in their own words;
- what was done about it, and what was tried and rejected on the way;
- what holds now, and what the record does not say.

Match the colleague's register: a neutral question gets a neutral answer; a
pointed one gets a calm, specific one, and specificity does the work. Do not
apologise for a decision the record supports, and do not volunteer blame it does
not assign. No ids and no hedging about what you looked at in this paragraph.

### b · The evidence

One line per claim in the reply, oldest first, each ending in its id; then the
computed absences word for word, then the scope line as each read printed it —
one per `receipts facts` read, never added together, since a summed line
describes a search nobody ran. This is what the user skims
before sending and forwards if the colleague pushes back. If the record does not
support a sentence of the reply, fix the reply rather than annotating this list.

### c · Before you send

List, one line each, what the record does not settle and the user should confirm
or add — for example whether a linked source says what the note says (the ledger
holds only the link), whether anyone approved the change (no record), or why a
node changed when its reason is `unstated`. Then ask whether the tone needs
adjusting, and stop. Do not send it, do not offer to send it, do not draft a
follow-up.

<example>
The colleague: "Why does the returns report suddenly exclude marketplace orders? Nobody asked me."

### a · The reply

> The returns report has excluded marketplace orders since 14 May. On 2 May the finance lead asked for it in writing: "returns should only count orders we fulfil ourselves, marketplace returns are the seller's". The change was made on 14 May and checked against April's figures the same day. Before that, tagging marketplace orders instead of dropping them was tried and set aside, because the tag was missing on a third of older orders. I can't see a record of who else was told about the change, which is fair to raise.

### b · The evidence

- 2 May: the finance lead's request, in their words. [#41]
- 14 May: the change to `returns.filter_orders`, by the task "exclude marketplace returns". [#e57]
- 14 May: checked against April's figures. [#o9]
- Tagging instead of dropping was rejected: the tag was missing on a third of older orders. [#44]
- Nobody being informed: not on record. [scope]

### c · Before you send

- Whether anyone outside finance was told — nothing records it.
- The finance lead's request links an email thread; the ledger holds only the link, never checked.
- Does the tone need adjusting?

Why this is right: the reply gives the whole situation in date order, with the request in the requester's own words; every claim has a line in b; what the record cannot settle is handed to the user in c instead of being guessed.
</example>

## What this skill never does

- It **never sends** the reply, or any message, anywhere.
- It never edits, creates or deletes anything in the repository, and it changes
  nothing the project depends on. `receipts candidates`, `receipts facts`, `graph`,
  `record` and `plan` write nothing; `why` appends a `read_hit` row per record it
  shows, an access log and nothing more.
- It never runs tests, builds, or any command other than the `provledger` reads
  above.
- It never fills a gap in the ledger with a plausible explanation: the gap goes in
  section c, for the user.
