"""The `/receipts` skill's contract (spec §8.5-8.8).

`/ledger` answers the user's own question about the project. `/receipts` helps
the user reply to a colleague. Both are read-only and both take their facts from
the ledger and nowhere else, so the two skills are held to the same bar — the
assertions here deliberately mirror `test_skill_bundle.py`.

What is pinned beyond that is the SHAPE of the output, because the shape is the
product decision (§8.6): the courteous reply comes FIRST, the evidence under it,
and the last thing on screen is a question about tone. A later edit that turns
this back into a pile of material with the reply at the bottom would be a
regression in the thing the user actually asked for, and prose regressions are
invisible unless something asserts them.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "receipts" / "SKILL.md"

# The reads it is allowed to run. The split is the point (§8.8 item 1): code
# offers candidates, THE SESSION MODEL picks, code then returns facts for the
# picks. A single call that both searched and chose would put the choosing back
# in a scoring function.
RECEIPTS_COMMANDS = ("provledger receipts candidates ", "provledger receipts facts ")

# The reads the model navigates with (spec §10.3). These are the difference between
# "we hand it assembled material" and "it decides what to look at": a folded graph it
# unfolds itself, a node's history, one record in full, and a task's steps — the last
# being the only way to reach a failure that a COMPLETED plan no longer shows.
NAVIGATION_READS = ("provledger graph ", "provledger why ", "provledger record ", "provledger plan ")


def test_the_receipts_skill_is_bundled():
    assert SKILL.exists(), "skills/receipts/SKILL.md must exist: /receipts is a named command (spec §8.5)"


def test_frontmatter_declares_the_slash_command_and_that_it_is_read_only():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md missing frontmatter"
    head = text.split("---")[1]
    assert "name: receipts" in head
    assert "/receipts" in head, "the slash-command trigger must be in the description"
    assert "read-only" in head, "a reader deciding whether to run it must see this before running it"


def test_it_names_the_two_reads_it_runs():
    text = SKILL.read_text(encoding="utf-8")
    for cmd in RECEIPTS_COMMANDS:
        assert cmd in text, f"the skill must name the command it runs: {cmd}"


def test_the_model_does_the_choosing_and_no_second_model_is_spawned():
    """§8.8. The session already has a model; a tool must not fork another."""
    text = SKILL.read_text(encoding="utf-8")
    assert "you are the model" in text.lower(), \
        "the skill must tell the model that IT does the choosing, not a scoring function"
    assert "--no-model" in text or "no second" in text.lower(), \
        "the skill must say that no extra model is called"


def test_the_output_is_reply_first_then_evidence_then_the_tone_question():
    """§8.6, in order. The order is the decision, so the order is asserted."""
    text = SKILL.read_text(encoding="utf-8")
    reply = text.find("## a ·")
    evidence = text.find("## b ·")
    tone = text.find("## c ·")
    assert -1 not in (reply, evidence, tone), "the three sections must be headed a / b / c"
    assert reply < evidence < tone, "reply must come before the evidence, and the tone question last"
    # and each section must be the thing it claims to be
    assert "reply" in text.lower()
    head = text[reply:evidence]
    assert "paste" in head.lower() or "copy" in head.lower(), \
        "section a is a finished reply the user can send, not a summary of one"


def test_it_never_sends_and_never_writes():
    text = SKILL.read_text(encoding="utf-8")
    assert "never send" in text.lower(), "the skill must state that it does not send the reply"
    assert "never edits, creates or deletes" in text, "same read-only sentence /ledger carries"
    assert "Never answer from memory" in text, "same anti-confabulation rule /ledger carries"


def test_it_says_an_absence_is_an_acceptable_reply():
    """The failure this whole feature exists to prevent is a confident reply
    resting on nothing. The instruction has to be explicit, because an unprompted
    model will paper over a gap rather than name it."""
    text = SKILL.read_text(encoding="utf-8")
    assert "no record" in text.lower(), \
        "the skill must tell the model that 'there is no record of that' is a usable reply"

def test_it_teaches_the_data_model_not_just_the_commands():
    """§8.9. The model decides for itself which history to read, so it has to
    know what the ledger holds and how the pieces relate. A skill that lists
    commands without the data model leaves it guessing what is even askable."""
    text = SKILL.read_text(encoding="utf-8")
    low = text.lower()
    for relation in ("upstream", "downstream"):
        assert relation in low, f"the model must be told the graph has {relation} relations to follow"
    assert "revision" in low, "tasks and their revisions are part of the data model"
    # the record kinds it can ask for, named so the model knows they exist
    assert "rejected" in low, "rejected paths are a record kind, and the answer to 'why not the other way'"
    assert "constraint" in low
    assert "failed" in low or "anti-pattern" in low, "a recorded failure is a record kind too"


def test_the_navigation_primitive_is_named_and_shown_as_iterative():
    """`provledger why` is the read the model navigates with: its header counts
    tell it whether a node is worth expanding, and its output names the command
    to expand with. The skill must point at it and must say the reading is a
    loop, not one shot — otherwise a model takes the first page and stops."""
    text = SKILL.read_text(encoding="utf-8")
    assert "provledger why " in text, "the navigation primitive must be named"
    low = text.lower()
    assert "--impact" in text or "--all" in text, "the skill must show how to expand a bounded read"
    assert "you decide" in low or "decide for yourself" in low or "you choose" in low, \
        "the skill must hand the choice of what to read to the model"
    assert "follow" in low or "iterat" in low, "reading the ledger is a loop, not a single call"


def test_it_does_not_pretend_the_entry_point_ranking_is_judgement():
    """`receipts candidates` is a starting point, not an answer. The defect this
    replaced (FL-145/FL-146) was a word-frequency score being treated as a
    decision about relevance."""
    text = SKILL.read_text(encoding="utf-8")
    assert "`why`" in text, "the skill must point at the provenance label, not the score"
    assert "score" in text.lower(), "it must say what the score is and is not good for"

def test_it_names_the_reads_the_model_navigates_with():
    """§10.3. A skill that names only the entry point leaves the model stuck at it."""
    text = SKILL.read_text(encoding="utf-8")
    for cmd in NAVIGATION_READS:
        assert cmd in text, f"the skill must name the read it navigates with: {cmd}"


def test_it_says_a_task_can_hide_its_own_failures():
    """The sharpest thing in the ledger and the easiest to miss: a plan whose
    failure was recovered closes COMPLETED, so the detour is gone from its own
    status and only the step rows remember. Seen on a real ledger: a COMPLETED
    plan holding FAILED steps, one of which carried the entire rationale for a
    constant someone later questioned. If the skill does not say this, a model
    reads the status and stops."""
    text = SKILL.read_text(encoding="utf-8")
    low = text.lower()
    assert "completed" in low, "the skill must name the status that hides failures"
    assert "recover" in low, "a recovered failure is why the status is misleading"
    assert "provledger plan " in text, "and the read that gets past it"


def test_it_warns_that_unreachable_is_not_absent():
    """FL-158: three times in one session — twice by me and once by another agent —
    "I searched and found nothing" was reported as "there is no record", while the
    answer sat in a column nobody had searched. A model that only searches the
    places it thinks of will state absence with confidence. The instruction has to
    be explicit, because the failure feels like diligence."""
    text = SKILL.read_text(encoding="utf-8")
    low = text.lower()
    assert "did not find" in low or "could not find" in low or "searched" in low, \
        "the skill must distinguish what was searched from what exists"
    assert "not the same" in low or "does not mean" in low or "≠" in text, \
        "the skill must say outright that unreachable is not absent"

def test_the_permitted_commands_rule_does_not_forbid_the_reads_the_skill_mandates():
    """A real defect caught by running the skill, not by reading it.

    The skill was written when the design had exactly two reads, and kept a hard
    rule saying "two commands, and no others". The navigation section added later
    documents four more AND makes two of them mandatory before asserting an
    absence. A fresh agent following the skill honoured the hard rule, did not run
    `provledger plan`, and lost the only material that answered the question it had
    been asked.

    The earlier assertions all passed, because they checked that a command was
    NAMED — not that the rules permitted running it. So this asserts the thing that
    actually broke: no rule may cap the command count below what the skill requires.
    """
    text = SKILL.read_text(encoding="utf-8")
    named = [c for c in (RECEIPTS_COMMANDS + NAVIGATION_READS) if c in text]
    assert len(named) >= 5, "sanity: the skill should name more than two reads by now"
    for forbidding in ("Two commands, and no others",
                       "two commands, and no others",
                       "and no others"):
        assert forbidding not in text, (
            f"the skill names {len(named)} reads but still carries the rule "
            f'"{forbidding}" — an agent that obeys it cannot do what the skill requires'
        )


def test_every_read_the_skill_requires_is_also_permitted():
    """The checklist before asserting an absence requires `plan` and `record`. Both
    must be permitted by the hard rules, not merely described later in the prose —
    an agent reads the rules as binding and the prose as guidance, so a read that
    appears only in the prose is a read it will not run."""
    text = SKILL.read_text(encoding="utf-8")
    rules = text[text.index("## The hard rules"):text.index("## What the ledger holds")]
    for name in ("plan", "record", "graph", "why"):
        assert f"`{name}`" in rules or f"provledger {name}" in rules, (
            f"the skill requires `{name}` later but the hard rules never permit it"
        )

def test_the_repo_is_permitted_as_an_index_and_forbidden_as_evidence():
    """The rule the user settled: this tool **supplements** the code rather than
    restricting it. The code shows only the winner — never the rejected option, who
    asked, or what it cost — and that remainder is what the ledger holds. So reading
    the repo is encouraged, and the one failure to prevent is narrow: reading the
    code and **inventing a reason**, when the real reason sits in the task history.

    Walked live: grep the constant, hand `file:line` to `why`, read the records,
    follow one to the task. An earlier wording ("no reading source files") forbade
    step one, and an agent that reads the rules as binding would not grep at all."""
    text = SKILL.read_text(encoding="utf-8")
    low = text.lower()
    assert "no reading source files" not in low, \
        "a blanket ban on reading source files forbids the normal way in"
    assert "read the code freely" in low or "read the code" in low, \
        "the skill must say outright that the code is the agent's to read"
    assert "invent a reason" in low, \
        "and must name the one failure to prevent: a reason invented from the code"
    assert "file:line" in low, "and show how a code location becomes a node"

def test_it_shows_how_to_get_from_a_record_to_the_task_that_made_it():
    """`provledger record '#<id>'` prints `plan <plan-id> · step <step-id>`. That
    is the hop from a citable record to the task whose steps and logs hold the
    substance, and it is the step a model will not take unless told the chain
    exists."""
    text = SKILL.read_text(encoding="utf-8")
    low = text.lower()
    assert "provledger plan " in text
    assert "names the plan" in low or "names the task" in low or "plan id" in low, \
        "the skill must say that a record points at the task that produced it"


def test_a_source_is_a_pointer_and_a_tier_is_part_of_what_a_record_says():
    """The release check's judge caught two replies saying more than the record:
    an email recorded as a link and never checked became 'the email attached ...
    happy to forward it', and an asserted instruction became 'what had been
    agreed'. The ledger holds a source's label, time and link — never its body —
    and an asserted record is the agent's reading, not an agreement."""
    text = " ".join(SKILL.read_text(encoding="utf-8").split())      # prose wraps; the phrase does not
    assert "never its body" in text
    assert "never write that the email is attached" in text
    assert "offer to send it" in text
    assert "not something anyone agreed" in text
    # asserted is a reading recorded as such — the agent's, or a note a person typed
    # (ledger-add records recorded_by human): saying "the agent's" made a reply call
    # a person's own note "an interpretation nobody confirmed"
    assert "the agent's or a person's" in text and "`recorded_by` says which" in text
    assert "not a quote" in text
    assert "an `asserted` record is the agent's reading" not in text


def test_the_reply_is_a_timeline_and_a_tasks_goal_is_not_a_nodes_reason():
    """The user's ruling on a real /receipts reply. Asked who signed off on a
    change to a node whose own reason was never recorded (`unstated`), the reply
    said "what's on the record is why the change was made" and gave the goal of
    the task that changed it. The task's goal is not the node's reason: say the
    task recorded none for it. A reason a person stated in their own words is
    theirs — give it as theirs and trust it. And the dates carry the reply (when
    it was said, when it changed), not the plan ids."""
    text = " ".join(SKILL.read_text(encoding="utf-8").split())
    assert "a task's goal is not a node's reason" in text.lower()
    assert "recorded no reason for it" in text
    assert "give it as theirs and trust it" in text
    assert "tell it as a timeline" in text.lower()
    assert "when it was said" in text and "when it changed" in text


def test_the_reply_is_complete_and_hands_back_what_the_record_does_not_settle():
    """The user's ruling (2026-10-06): /receipts speaks to someone else on the
    user's behalf, so it aims at a complete answer — what it is, who asked and in
    what words, what was done, what holds now — and lists, apart from the reply,
    what the user must confirm or add before sending."""
    text = " ".join(SKILL.read_text(encoding="utf-8").split())
    assert "who asked, when, in their own words" in text
    c = text[text.index("### c · Before you send"):]
    assert "confirm or add" in c
    assert "tone" in c.lower(), "the tone question still closes it"
    assert "A claim you cannot point at does not go in the reply — it goes in section c" in text


def test_one_complete_generic_example_with_why_it_is_right():
    import re
    examples = re.findall(r"<example>(.*?)</example>", SKILL.read_text(encoding="utf-8"), re.S)
    assert len(examples) >= 1 and all("Why this is right:" in e for e in examples)
    joined = " ".join(examples).lower()
    for word in ("discount", "orders feed", "rollup", "load_orders", "weekly_report", "list_price"):
        assert word not in joined, f"a shipped example must not carry the release check's answers: {word}"


def test_the_tiers_are_defined_by_the_legend_not_here():
    text = " ".join(SKILL.read_text(encoding="utf-8").split())
    assert "legend" in text.lower()
    assert "`stated` = the user's own recorded words" not in text, "one definition, in the CLI legend"
