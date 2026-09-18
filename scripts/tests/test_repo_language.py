"""The repository is written in English, and the exceptions are named.

The rule: a file tracked by git is in English, or it is not tracked at all.
Working notes, local plans and the full deferred-work ledger stay on the author's
machine behind `.gitignore`; what ships is readable by everyone who clones it.

The one exception is a product feature, not a leftover. The dashboard serves a
Chinese UI under `?lang=zh` — English is the default — and the phrases it prints
have to live somewhere in the source. They live in `vocab.py`, in the templates
that branch on `lang`, in the one backend module that renders a scope line in
both languages, and in the Chinese keyword list the external-artifact judge needs
in order not to be half-blind to a person writing in Chinese. A handful of test
files come with it, because the ledger stores what a person actually said, in
the language they said it, and something has to prove that text survives the
round trip. Every one of those files is listed below with the reason it is
there.

A new file with Chinese in it fails this test. That is the point: the whitelist
is a decision, and adding to it should be a decision too.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

CJK = tuple(
    range(0x3400, 0x4DC0)  # CJK Extension A
) + tuple(
    range(0x4E00, 0xA000)  # CJK Unified Ideographs
)
CJK_SET = frozenset(chr(c) for c in CJK)

# path -> why this file is allowed to contain Chinese.
ALLOWED: dict[str, str] = {
    # ── the published walkthrough ────────────────────────────────────────────
    "docs/walkthrough.html":
        "The walkthrough's captions ship in both languages behind the same "
        "switch the dashboard uses: English by default, Chinese on request. "
        "The Chinese here is the zh caption track — product copy, the same "
        "kind of thing as vocab.py — not prose about the code.",
    # ── the vocabulary table and the pages it feeds ──────────────────────────
    "orchestrator-webapp/app/vocab.py":
        "The term table itself: every ledger token paired with the English and "
        "the Chinese phrase the page prints. This IS the ?lang=zh feature, and "
        "its docstring explains why English is the default.",
    "orchestrator-webapp/tests/test_vocab.py":
        "Asserts on that table — that every value has an entry in both columns "
        "and that the five tiers stay five distinguishable things in Chinese.",
    "orchestrator-webapp/app/templates/_dashboard_partial.html":
        "Dashboard template: Chinese only inside `lang == 'zh'` branches.",
    "orchestrator-webapp/app/templates/dashboard.html":
        "Dashboard template: Chinese only inside `lang == 'zh'` branches.",
    "orchestrator-webapp/app/templates/graph.html":
        "Graph template: Chinese only inside `lang == 'zh'` branches.",
    "orchestrator-webapp/app/templates/node.html":
        "Node template: Chinese only inside `lang == 'zh'` branches.",

    # ── the two backend modules that are bilingual on purpose ───────────────
    "orchestrator-backend/orchestrator/ask/scope.py":
        "`scope.line(lang='zh')` renders the same scope sentence in Chinese "
        "for the ?lang=zh pages; the English branch sits right next to it.",
    "orchestrator-backend/orchestrator/external_trigger.py":
        "`ARTIFACT_WORDS_ZH` — an English deck and a Chinese one are the same "
        "artifact, and a judge that knows only one of them is silently "
        "half-blind. The word list is the feature.",

    # ── tests that prove non-English text survives the whole pipeline ───────
    # The ledger stores what a person actually said, in the language they said
    # it. These suites pin that: a Chinese utterance must round-trip through
    # storage, the page and the answer filter without being mangled or dropped.
    "orchestrator-backend/tests/test_ask_facts.py":
        "Asserts the Chinese scope line that scope.line(lang='zh') produces.",
    "orchestrator-backend/tests/test_ask_model_path.py":
        "Pins that the answer filter never deletes a correct sentence because "
        "of the script it is written in (Chinese, Japanese, Korean).",
    "orchestrator-webapp/tests/test_history_first.py":
        "Stores a verbatim Chinese utterance and checks the page renders it "
        "back unchanged.",
    "orchestrator-webapp/tests/test_ledger_route.py":
        "Bilingual /ledger assertions: the same question answered under ?lang= "
        "en and zh, and a correct Chinese sentence that must not be deleted.",
    "orchestrator-webapp/tests/test_node_timeline.py":
        "Chinese constraint and utterance fixtures that the node timeline must "
        "render verbatim.",

    # ── the guard that keeps the Chinese UI free of blame words ─────────────
    "orchestrator-backend/tests/test_wording.py":
        "Holds the Chinese blame-words that must never reach the ?lang=zh UI, "
        "and the source-level pair it asserts on. The words being searched for "
        "have to be spelled out somewhere.",
}


def _tracked_text_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def _chinese_lines(path: Path) -> list[int]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []          # binary fixture (a .pptx, a .png) — not prose
    return [i for i, line in enumerate(text.splitlines(), 1)
            if CJK_SET.intersection(line)]


def test_every_tracked_file_is_english_or_on_the_named_whitelist() -> None:
    offenders: dict[str, list[int]] = {}
    for rel in _tracked_text_files():
        if rel in ALLOWED:
            continue
        lines = _chinese_lines(REPO / rel)
        if lines:
            offenders[rel] = lines[:10]
    assert not offenders, (
        "These tracked files contain Chinese. Either write them in English, or "
        "gitignore them, or — if the Chinese is a product feature like the "
        "?lang=zh dashboard — add the path to ALLOWED in this file together "
        "with the reason:\n"
        + "\n".join(f"  {p}: lines {ls}" for p, ls in sorted(offenders.items()))
    )


def test_the_whitelist_has_no_stale_entries() -> None:
    """A whitelisted file that no longer has Chinese should leave the list, so
    the list keeps meaning what it says."""
    tracked = set(_tracked_text_files())
    stale = [p for p in ALLOWED
             if p not in tracked or not _chinese_lines(REPO / p)]
    assert not stale, (
        "ALLOWED lists files that are gone or no longer contain Chinese; "
        f"remove them: {sorted(stale)}"
    )


def test_every_whitelist_entry_states_a_reason() -> None:
    thin = [p for p, why in ALLOWED.items() if len(why) < 40]
    assert not thin, f"whitelist entries without a real reason: {sorted(thin)}"
