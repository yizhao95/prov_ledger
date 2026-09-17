# provLedger · North Star

In one sentence: **every change and every number can be traced back to the
decision that put it there, and a person can see that decision — and overrule
it — in thirty seconds.**

## The three cores (the product is these three; any one missing and it is incomplete)

1. **A project database that sees the whole graph and the whole history.**
   Code, data, metrics and external objects live in one graph. Every node
   carries a history the system computed, reasons labelled with their source
   level, anchored constraints, and a timeline a hash chain vouches for.
2. **A dashboard a human can audit.**
   Three views — Graph, Node, Task — over one context triple (project, node,
   at), switching between them without losing your place. They answer three
   questions: why did this thing change, who was shown it, and who decided
   something because of it.
3. **A door for declaring the real world.**
   The user says one sentence; an LLM puts the meeting decision, the external
   system, the business rule or the hand-computed number into the graph. From
   then on it has exactly the same provenance as a code node.

## Principles that do not move

- **The graph's structure and its changes are computed.** A node's data comes
  in several kinds: observed by the system, derived by the system, asserted by
  an LLM, stated by the user. All of them are recorded and kept apart by tier.
  Nothing is blended, and nothing is excluded.
- **Observation and inference stay distinguishable, always.** The system
  decides the tier from the source (observed / derived / asserted / stated /
  unstated). An LLM may not award itself a tier and may not produce a result
  number.
- **Loading is progressive.** We do not hand the LLM every record. It gets
  summaries and counts first and decides what to expand; the expansion rules
  live in the skill. Whatever is cut always reappears as a count.
- **Never silent.** No information means saying so. Trimming means showing the
  count. An LLM judgement means leaving a trace that can be calibrated. A tool
  built to kill silent failures cannot have silent failures of its own.
- **History is append-only.** Changing something means appending a supersede.
- **The system can record "was shown" and "was adopted" — never "was read".**
  A display count is not evidence that anyone read anything.
- **Neutral wording.** Decision provenance, source, context. Not evidence, not
  accountability, not blame.
- **The personal / shareable boundary is enforced by code.**
- **The tool may not get slower in silence.** Every round trip has to answer
  "which tool call did this cost?", and the threshold is a test.
- **Do not overreach.** Our skills are part of the plugin's integrity and may
  be mandatory, but they govern only how a plan is written and how it is
  closed; they do not touch the user agent's other skills, tools or settings.
  Hooks only add, never veto: by default they record and display, they do not
  block, and blocking is an option a person turns on. Some interference is
  unavoidable, so a test pins it down: against a Claude Code running
  superpowers alone on the same task, the extra tool rounds and extra context
  have a ceiling (the H group).
- **The value is in three places.** Store the decision; turn the work and its
  history into an auditable account; and when a past decision is triggered,
  use the hit count and this change's reason to draw the line "the plan changed
  because of it" explicitly.

## How to use this: run every change, every document and every pitch past it

- Which of the three cores does this change make more complete? If you cannot
  say, do not do it.
- Does it break any principle above?
- The README, the demo and the pitch tell the story in the order 1 → 2 → 3,
  never in implementation terms like "orchestrator" or "contract gate".

## Where this came from

Settled in a brainstorm with the user on 2026-09-14; see §14 and §19 of
`docs/superpowers/specs/2026-09-14-decision-provenance-design.md` (a local
working document, not published with the repository).

## In plain words (the opening for the README, the demo and the pitch)

It is a project memory and an auditor that lives inside Claude Code. You and an
agent change code, change data, change the numbers in a report, and it keeps
the books alongside you: what changed (computed by comparing before and after
itself, not taken on the agent's word), why it changed (your own words, the
agent's explanation, or something a rule derived), what it rested on (the link
to the email, the meeting, the ticket — or just the thing said that day), and
how it turned out later.

It opens the books at two moments. **Before the change**: when the agent is
about to touch something, it looks that thing up first and puts a headline at
the top of the plan — "you said not to change this that way", "last time this
change broke something downstream", "two things downstream depend on it". The
agent can comply or go around it with a reason; going around leaves a trace,
and the outcome comes back into the record. **When you want to look**: the
dashboard switches between three points of view — which node in the whole graph
has a story behind it; open a node to see how it changed over time, why each
time, who was shown it, and who changed their plan because of it; open a task
to see what it read and what it decided. And there is a door for putting things
that are not code into the graph with one sentence.

In one line: `git blame` tells you who wrote it. This tells you why — and the
next time someone moves to overturn that why, it speaks up first.

There are only four pieces of technology. Python's own parser computes the
nodes and the changes. Two append-only, hash-chained SQLite files record
everything. Claude Code's hooks listen for what was actually said, count the
cost, and put in a word before an edit. Two skills tell the agent to read the
books before writing a plan and to fill in the why when closing one. The model
does exactly two things: match words to changes, and judge when information is
missing. Every judgement is marked as inference and leaves a trace. It never
produces a result number, and it can never label its own words as yours.
