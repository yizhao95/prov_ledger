"""vocab — the ledger keeps its terms, people read theirs (DP phase 2d, Task 3d).

`headline`, `read_hit`, `influence`, `evidence_level`, `tier='asserted'` are
precise words and they are the SCHEMA's words. Renaming the columns to make the
page friendlier would destroy the thing the columns exist for, so nothing is
renamed: this module is a one-way table from the ledger's token to a sentence a
person can read, applied only to text a person reads.

Three rules hold this together:

1. **Every value has an entry.** A missing word is a test failure, not a silent
   English token sitting inside a Chinese sentence where nobody notices. An
   unknown value is echoed AND reported by `is_fallback`, so it can be found.
2. **The distinctions survive the translation.** The five tiers must still read
   as five different things; "系统观测到" and "agent 的判断" may never collapse
   into "记录". If a translation blurs observation and inference, it is wrong
   no matter how friendly it sounds.
3. **Machine attributes keep the original token.** `data-tier="asserted"` stays
   `asserted`. The wording is for the reader; the DOM is for the tests, the
   ETag and anyone reading the page as data.

Ids are the same story: a reader wants the node's NAME and the task's TITLE, so
the id moves into `title=` and `data-*` rather than out of existence.
"""
from __future__ import annotations

LANGS = ("zh", "en")
DEFAULT_LANG = "zh"

# kind -> token -> {zh, en}
WORDS: dict[str, dict[str, dict[str, str]]] = {
    # how the system decided this row's provenance — never chosen by an LLM
    "tier": {
        "observed": {"zh": "系统观测到", "en": "system observed"},
        "derived": {"zh": "系统推出", "en": "system derived"},
        "asserted": {"zh": "agent 的判断", "en": "agent's reading"},
        "stated": {"zh": "你的原话", "en": "your own words"},
        "unstated": {"zh": "未说明", "en": "not stated"},
    },
    # a finding never blocks; the severity says how loudly it asks
    "severity": {
        "blocking": {"zh": "必须回应", "en": "needs an answer"},
        "warning": {"zh": "值得注意", "en": "worth a look"},
        "info": {"zh": "仅供参考", "en": "for information"},
    },
    # how checkable the source is — computed, never stored
    "evidence_level": {
        "linked": {"zh": "有链接可查", "en": "a link you can open"},
        "verbal": {"zh": "有原话", "en": "the words themselves"},
        "task_context": {"zh": "只有任务脉络", "en": "only the task around it"},
        "unstated": {"zh": "未说明", "en": "nothing recorded"},
    },
    "role": {
        "reason": {"zh": "原因", "en": "why it changed"},
        "constraint": {"zh": "规矩", "en": "rule"},
        "rejected_path": {"zh": "走不通的路", "en": "path already ruled out"},
    },
    "event": {
        "node_added": {"zh": "新增", "en": "added"},
        "node_changed": {"zh": "改动", "en": "changed"},
        "node_renamed": {"zh": "改名", "en": "renamed"},
        "node_moved": {"zh": "移动", "en": "moved"},
        "column_dropped": {"zh": "列被移除", "en": "column removed"},
        "node_removed": {"zh": "被删除", "en": "deleted"},
        "removed": {"zh": "被删除", "en": "deleted"},
        "identity_asserted": {"zh": "身份由模型判定", "en": "identity judged by a model"},
        # the two quiet ones read identically on purpose: they are what gets folded
        "node_matched": {"zh": "无变化", "en": "no change"},
        "identity_kept": {"zh": "无变化", "en": "no change"},
    },
    # when a record was SHOWN — never summed across moments
    "moment": {
        "plan": {"zh": "写计划时", "en": "while planning"},
        "edit": {"zh": "动手改之前", "en": "before the edit"},
        "close": {"zh": "收尾时", "en": "at close"},
        "why": {"zh": "有人来查时", "en": "when someone asked"},
    },
    "via": {
        "headline_response": {"zh": "回应提醒时引用", "en": "cited answering a finding"},
        "reason_because": {"zh": "写理由时引用", "en": "cited in a reason"},
        "constraint_ack": {"zh": "认下这条规矩", "en": "rule acknowledged"},
    },
    "status": {
        "PENDING": {"zh": "还没开始", "en": "not started"},
        "IN_PROGRESS": {"zh": "进行中", "en": "in progress"},
        "COMPLETED": {"zh": "做完了", "en": "done"},
        "FAILED": {"zh": "失败了", "en": "failed"},
        "NEEDS_REVIEW": {"zh": "等复核", "en": "awaiting review"},
    },
    # the nouns of the system itself
    "term": {
        "headline": {"zh": "开工前的提醒", "en": "the heads-up before you start"},
        "read_hit": {"zh": "被看到", "en": "shown"},
        "influence": {"zh": "改变了计划", "en": "changed the plan"},
        "node": {"zh": "这个东西", "en": "this thing"},
        "plan": {"zh": "任务", "en": "task"},
        "run": {"zh": "第几次分析", "en": "analysis run"},
        "graph": {"zh": "整张图", "en": "the whole map"},
        "reduced": {"zh": "精简视图", "en": "reduced view"},
    },
}


def lang_of(value: str | None) -> str:
    """The language to render in. Anything unrecognised is the default, because a
    typo in a query string should not blank out the page."""
    return value if value in LANGS else DEFAULT_LANG


def is_fallback(token, *, kind: str, lang: str = DEFAULT_LANG) -> bool:
    """True when `token` has no entry — the caller (and the test suite) can see
    that the word came back untranslated instead of it passing unnoticed."""
    if token in (None, ""):
        return False
    return str(token) not in WORDS.get(kind, {})


def say(token, *, kind: str, lang: str = DEFAULT_LANG) -> str:
    """The reader's word for one of the ledger's tokens. An empty token is an
    empty string; an unknown one comes back as itself (and `is_fallback` says so)."""
    if token in (None, ""):
        return ""
    row = WORDS.get(kind, {}).get(str(token))
    if not row:
        return str(token)
    return row.get(lang_of(lang)) or row[DEFAULT_LANG]


# ── counts that read as sentences ────────────────────────────────────────────
# "read_hit: 0" is a fact nobody can act on. These say what the number means, and
# they keep 被看到 and 改变了计划 apart: the system can record that something was
# SHOWN, never that anyone read it.

def shown(n: int | None, lang: str = DEFAULT_LANG) -> str:
    n = int(n or 0)
    if lang_of(lang) == "en":
        return "never shown" if n == 0 else f"shown {n}×"
    return "没有被看到过" if n == 0 else f"被看到 {n} 次"


def adopted(n: int | None, lang: str = DEFAULT_LANG) -> str:
    n = int(n or 0)
    if lang_of(lang) == "en":
        return "changed no plan" if n == 0 else f"changed {n} plan(s)"
    return "没有改变过任何计划" if n == 0 else f"改变了 {n} 次计划"


def adopted_by(title: str, lang: str = DEFAULT_LANG) -> str:
    """"被 <plan> 采用" said the way a person would say it."""
    if lang_of(lang) == "en":
        return f"this record changed the plan of “{title}”"
    return f"这条记录改变了《{title}》的计划"


def folded(n: int | None, lang: str = DEFAULT_LANG) -> str:
    n = int(n or 0)
    if lang_of(lang) == "en":
        return f"{n} more with no change"
    return f"还有 {n} 次无变化的匹配"
