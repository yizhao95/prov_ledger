"""The source-mention word list (A3), and the one function that matches it.

A sentence like "Sarah emailed that Q3 excludes EMEA" has already named where the
constraint came from. Pinning the pointer at that moment costs one line of context;
pinning it a week later costs a search through somebody's mailbox. So the
UserPromptSubmit hook reads the sentence, and when it names an outside source it
asks for the pointer.

Two things this is deliberately not:

  It is not a judgement. Which email, which meeting, which ticket — that is a
  reading of the world, and it stays with the host agent, which has the tools and
  the credentials for it. This file only notices that a source was named.

  It is therefore not a model feature, and needs no calibration gate. It is
  substring matching over a list a person can read, disagree with, and edit. What
  comes back from the host is validated on its way in; nothing here guesses.

The list lives in its own module rather than inline in the hook so that it can be
imported, asserted on and argued with. Two copies of a word list become two
different word lists.

Matching is case-folded substring matching, which is why the entries are short
stems: `email` covers emailed / emails / emailing, `jira` covers the `JIRA-1234`
the design wrote down. Ticket keys of other shapes (`DATA-42`) are deliberately
NOT matched by pattern — a pattern loose enough to catch them also catches
`UTF-8` and `COVID-19`, and a hint that fires on nothing is worse than a hint that
occasionally stays quiet. `ticket` and `工单` carry that case instead.

False positives are cheap here and false negatives are not: the hint adds one line
and never blocks anything, so the list errs towards firing.
"""
from __future__ import annotations

# Order matters only in what `mentions_a_source` reports back: the first entry
# found in the sentence is the one named in the hint.
SOURCE_WORDS = (
    # ── English ──
    "email",            # also emailed / emails / emailing
    "e-mail",
    "in the thread",
    "on the thread",
    "in the chain",
    "on the call",
    "in the call",
    "in the meeting",
    "at the meeting",
    "meeting notes",
    "the minutes",
    "ticket",
    "jira",
    "confluence",
    "said in",
    "wrote in",
    "told me",
    "in slack",
    "on slack",
    "in teams",
    "on teams",
    # ── Chinese ──
    # The user writes in both languages, often in one sentence, so a list that read
    # only English would go quiet exactly where it is most useful.
    "邮件",              # email
    "会上",              # in the meeting
    "会议记录",           # meeting notes
    "纪要",              # the minutes
    "群里",              # in the group chat
    "聊天记录",           # chat log
    "工单",              # ticket
)


def mentions_a_source(text: str | None) -> str | None:
    """The first word from SOURCE_WORDS the sentence contains, or None.

    Returns the word rather than a bare True so the hint can say what it noticed;
    a hint that cannot explain itself is a hint nobody can tune.
    """
    if not isinstance(text, str) or not text:
        return None
    folded = text.lower()
    for word in SOURCE_WORDS:
        if word in folded:
            return word
    return None
