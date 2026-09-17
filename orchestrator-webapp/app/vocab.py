"""vocab — the ledger keeps its terms, the page states them plainly.

`headline`, `read_hit`, `influence`, `evidence_level`, `tier='asserted'` are
precise words and they are the SCHEMA's words. Renaming the columns to make the
page friendlier would destroy the thing the columns exist for, so nothing is
renamed: this module is a one-way table from the ledger's token to the phrase
the page prints, applied only to text a person reads.

**Register.** The first draft of this table overcorrected into conversational
Chinese ("因历史而变的决定", "还管着它的规矩"). The page is an audit surface: the
register is professional, precise and restrained — readable without being
chatty. A count states a measurement; it does not narrate one.

**Language.** This module is the vocabulary table behind the dashboard's Chinese
UI, and it is the one file in the package where Chinese belongs. English is the
default: every page renders in English unless the request asks for another
language, and Chinese is served only under `?lang=zh`, in the same professional
register. Chinese anywhere else — a comment, a docstring, a phrase a template
prints without a `lang` check — is a leak an English reader would see, not a
feature, and the place to fix it is here or at the `lang` branch that should
have guarded it.

Three rules hold this together:

1. **Every value has an entry in both columns.** A missing phrase is a test
   failure, not a silent English token sitting in a Chinese sentence. An unknown
   value is echoed AND reported by `is_fallback`, so it can be found.
2. **The distinctions survive the translation.** The five tiers must still read
   as five different things; `system-observed` and `model-asserted` may never
   collapse into "recorded". A phrasing that blurs observation and inference is
   wrong however readable it sounds.
3. **Machine attributes keep the original token.** `data-tier="asserted"` stays
   `asserted`. The wording is for the reader; the DOM is for the tests, the
   ETag and anyone reading the page as data.

Ids follow the same logic: a reader wants the node's name and the task's title,
so the id moves into `title=` and `data-*` rather than out of existence.
"""
from __future__ import annotations

LANGS = ("en", "zh")
DEFAULT_LANG = "en"

# kind -> token -> {en, zh}
WORDS: dict[str, dict[str, dict[str, str]]] = {
    # how the system decided this row's provenance — never chosen by an LLM
    "tier": {
        "observed": {"en": "observed", "zh": "系统观测"},
        "derived": {"en": "derived", "zh": "系统推导"},
        "asserted": {"en": "asserted", "zh": "模型断言"},
        "stated": {"en": "stated", "zh": "用户陈述"},
        "unstated": {"en": "unstated", "zh": "未陈述"},
    },
    # DP phase 2c: the five kinds of object a person can declare. Five different
    # things in both columns — "external system" and "external dataset" must not
    # collapse into "external", or the reader loses the distinction the type carries.
    "declared_type": {
        "external_system": {"en": "external system", "zh": "外部系统"},
        "business_rule": {"en": "business rule", "zh": "业务规则"},
        "stakeholder_decision": {"en": "stakeholder decision", "zh": "相关方决定"},
        "external_dataset": {"en": "external dataset", "zh": "外部数据集"},
        "manual_figure": {"en": "hand-computed figure", "zh": "手算数字"},
    },
    # a finding never blocks; the severity states what it asks of the reader
    "severity": {
        "blocking": {"en": "Blocking", "zh": "需响应"},
        "warning": {"en": "Warning", "zh": "需关注"},
        "info": {"en": "Info", "zh": "参考"},
    },
    # how checkable the source is — computed, never stored
    "evidence_level": {
        "linked": {"en": "linked", "zh": "可核对链接"},
        "verbal": {"en": "verbal", "zh": "原话记录"},
        "task_context": {"en": "task-context", "zh": "仅任务上下文"},
        "unstated": {"en": "unstated", "zh": "未陈述"},
    },
    "role": {
        "reason": {"en": "reason", "zh": "变更原因"},
        "constraint": {"en": "constraint", "zh": "生效约束"},
        "rejected_path": {"en": "rejected alternative", "zh": "已否决方案"},
    },
    "event": {
        "node_added": {"en": "added", "zh": "新增"},
        "node_changed": {"en": "changed", "zh": "变更"},
        "node_renamed": {"en": "renamed", "zh": "重命名"},
        "node_moved": {"en": "moved", "zh": "移动"},
        "column_dropped": {"en": "column removed", "zh": "列移除"},
        "node_removed": {"en": "removed", "zh": "移除"},
        "removed": {"en": "removed", "zh": "移除"},
        "identity_asserted": {"en": "identity asserted", "zh": "身份由模型断言"},
        # the two quiet ones read identically on purpose: they are what gets folded
        "node_matched": {"en": "unchanged", "zh": "无变更"},
        "identity_kept": {"en": "unchanged", "zh": "无变更"},
    },
    # when a record was SHOWN — never summed across moments
    "moment": {
        "plan": {"en": "at planning", "zh": "计划时"},
        "edit": {"en": "before edit", "zh": "编辑前"},
        "close": {"en": "at close", "zh": "关闭时"},
        "why": {"en": "on query", "zh": "查询时"},
    },
    "via": {
        "headline_response": {"en": "cited in a check response", "zh": "前置检查回应中引用"},
        "reason_because": {"en": "cited in a change reason", "zh": "变更原因中引用"},
        "constraint_ack": {"en": "constraint acknowledged", "zh": "约束已确认"},
    },
    "status": {
        "PENDING": {"en": "pending", "zh": "待执行"},
        "IN_PROGRESS": {"en": "in progress", "zh": "执行中"},
        "COMPLETED": {"en": "completed", "zh": "已完成"},
        "FAILED": {"en": "failed", "zh": "失败"},
        "NEEDS_REVIEW": {"en": "awaiting review", "zh": "待复核"},
    },
    # the nouns of the system itself
    "term": {
        "headline": {"en": "Findings", "zh": "计划前置检查"},
        "read_hit": {"en": "Surfaced", "zh": "呈现"},
        "influence": {"en": "Decisions relied on", "zh": "依据的历史记录"},
        "node": {"en": "Node", "zh": "节点"},
        "plan": {"en": "Task", "zh": "计划"},
        "run": {"en": "run", "zh": "分析批次"},
        "graph": {"en": "Graph", "zh": "依赖图"},
        "reduced": {"en": "Reduced view", "zh": "精简视图"},
        "ledger": {"en": "Ledger", "zh": "账本问答"},
    },
}

# The page's own strings. They live here rather than in the templates so a phrase
# cannot be added in one language and silently left untranslated in the other —
# the suite walks this table and fails on a missing column.
UI: dict[str, dict[str, str]] = {
    "prior_decisions": {"en": "Decisions relied on", "zh": "依据的历史记录"},
    "prior_decisions_empty": {"en": "No prior decisions relied on.", "zh": "本计划未采纳任何历史记录。"},
    "active_constraints": {"en": "Constraints", "zh": "生效约束"},
    "no_active_constraints": {"en": "None recorded.", "zh": "该节点上没有生效约束。"},
    "change_summary": {"en": "Recent", "zh": "变更摘要"},
    "change_history": {"en": "Change history", "zh": "变更历史"},
    "no_significant_change": {"en": "No significant change recorded.", "zh": "该节点尚无显著变更记录。"},
    "dependencies": {"en": "Dependencies", "zh": "依赖关系"},
    "node_ledger": {"en": "Node", "zh": "节点履历"},
    "read_only": {"en": "read-only", "zh": "只读"},
    "home": {"en": "Home", "zh": "首页"},
    "history": {"en": "History", "zh": "历史"},
    "outcomes": {"en": "Outcomes", "zh": "后果"},
    "show_history": {"en": "Show history", "zh": "查看历史"},
    "latest_plan": {"en": "Latest plan", "zh": "最新计划"},
    "viewing_historical": {"en": "Viewing a historical plan", "zh": "正在查看历史计划"},
    "skills_activated": {"en": "Skills", "zh": "已启用技能"},
    "original_instruction": {"en": "Instruction", "zh": "原始指令"},
    "verbatim": {"en": "verbatim", "zh": "逐字"},
    "context_estimate": {"en": "Context", "zh": "上下文估算"},
    "reasons_panel": {"en": "Reasons", "zh": "变更原因"},
    "upstream": {"en": "upstream", "zh": "上游"},
    "downstream": {"en": "downstream", "zh": "下游"},
    "records": {"en": "records", "zh": "条记录"},
    "analysis_runs": {"en": "runs", "zh": "次分析"},
    "because": {"en": "Reason", "zh": "原因"},
    "not_recorded": {"en": "No reason recorded.", "zh": "变更时未记录原因。"},
    "back_to_task": {"en": "Open task", "zh": "打开来源任务"},
    "open_node": {"en": "Open node", "zh": "打开节点"},
    "recorded_by": {"en": "by", "zh": "记录者"},
    "unchanged_matches": {"en": "Unchanged matches", "zh": "无变更匹配"},
    "minor_records": {"en": "minor", "zh": "次要记录"},
    "filtered_out": {"en": "filtered out", "zh": "被当前筛选排除"},
    "expand": {"en": "Show all", "zh": "全部展开"},
    "no_match": {"en": "No records match this filter.", "zh": "没有符合当前筛选的记录。"},
    "chip_significant": {"en": "Significant", "zh": "显著变更"},
    "chip_all": {"en": "All", "zh": "全部（含无变更）"},
    "chip_adopted": {"en": "Adopted", "zh": "仅被采纳的"},
    "chip_7d": {"en": "Last 7 days", "zh": "最近 7 天"},
    "chip_30d": {"en": "Last 30 days", "zh": "最近 30 天"},
    "chip_stated": {"en": "Stated", "zh": "用户陈述"},
    "search_placeholder": {"en": "Search records", "zh": "检索原话、变更原因、约束"},
    "search": {"en": "Search", "zh": "检索"},
    "search_hint": {"en": "Enter a term. Covers recorded words, reasons and constraints.",
                    "zh": "输入检索词。范围为原话记录、变更原因与约束。"},
    "search_empty": {"en": "No records match.", "zh": "没有找到包含该词的记录。"},
    "search_grouped": {"en": "grouped by node", "zh": "按记录所属节点分组"},
    "search_degraded": {"en": "The read-only connection cannot build the full-text index, so this is a substring "
                              "match and may miss inflected forms. Use `provledger why --search` for the full index.",
                        "zh": "只读连接无法建立全文索引，此处为子串匹配，可能漏掉词形变化。完整检索请用 `provledger why --search`。"},
    "expand_full": {"en": "Expand", "zh": "展开完整图"},
    "reduced_view": {"en": "Reduced view — {drawn} nodes with records and their direct dependencies · full graph {total} nodes",
                     "zh": "精简视图：仅含有记录的节点及其直接依赖；完整图 {total} 个节点，可展开。"},
    "graph_nodes_drawn": {"en": "{drawn} of {selected} selected · full graph {total}",
                          "zh": "已绘制 {drawn} / 选中 {selected} / 全图 {total}"},
    "focus_not_found": {"en": "Focus {focus} is not in this graph — showing the full graph.",
                        "zh": "焦点 {focus} 不在此图中，改为显示完整图。"},
    "mode_focus": {"en": "Focus", "zh": "焦点"},
    "mode_story": {"en": "Story", "zh": "有记录"},
    "mode_data": {"en": "Data flow", "zh": "数据"},
    "mode_full": {"en": "Full", "zh": "完整"},
    "no_focus": {"en": "No focus node in context", "zh": "当前上下文没有焦点节点"},
    "graph_unavailable": {"en": "State graph unavailable", "zh": "状态图不可用"},
    "back": {"en": "Back", "zh": "返回"},
    "view_source": {"en": "View source", "zh": "查看出处"},
    "not_reachable": {"en": "Not reachable", "zh": "不可查看"},
    "clusters": {"en": "{clusters} clusters · {nodes} nodes", "zh": "{clusters} 个簇 · {nodes} 个节点"},
    "covered_by": {"en": "covered by constraint", "zh": "受约束覆盖"},
    "plans": {"en": "plans", "zh": "个计划"},
    "downstream_consumers": {"en": "Downstream consumers", "zh": "下游消费者"},
    "upstream_sources": {"en": "Upstream", "zh": "上游"},
    # DP phase 2e (Task 4): /ledger — ask the ledger, read-only.
    "ask_title": {"en": "Ask the ledger", "zh": "问一句账本"},
    "ask_placeholder": {"en": "Why was this done? Did we try the alternative?",
                        "zh": "为什么这样做？我们试过别的吗？"},
    "ask_button": {"en": "Ask", "zh": "提问"},
    "ask_hint": {"en": "A question in words. The nodes and the facts are computed; a model may only restate them, "
                       "and every sentence cites the record it rests on.",
                 "zh": "用一句话提问。节点与事实由代码算出；模型只能转述，且每句都要引用它所依据的记录。"},
    "ask_answer": {"en": "Answer", "zh": "答案"},
    "ask_absences": {"en": "Not in the ledger", "zh": "账本里没有的"},
    "ask_scope": {"en": "Scope of the search", "zh": "检索范围"},
    "ask_records": {"en": "Open records", "zh": "展开记录"},
    "ask_export": {"en": "Export card", "zh": "导出证据卡"},
    "ask_no_model": {"en": "summary unavailable: no model configured", "zh": "无法生成总结：没有配置模型"},
    "ask_no_model_help": {"en": "The fact table below is the answer. Set PROVLEDGER_ASK_RUNNER=claude to let the "
                                "dashboard call a model, or run `provledger ask` in a terminal.",
                          "zh": "下面的事实表就是答案。要让面板调用模型，设置 PROVLEDGER_ASK_RUNNER=claude；或在终端运行 `provledger ask`。"},
    "ask_dropped": {"en": "{n} sentence(s) dropped", "zh": "剔除 {n} 句"},
    "ask_nothing_kept": {"en": "No sentence survived the checks.", "zh": "没有一句通过校验。"},
    "ask_not_logged": {"en": "This question was not recorded: the ledger is open read-only here.",
                       "zh": "本次提问未被记录：此处的账本是只读打开的。"},
    "ask_wrong": {"en": "Wrong? Record it:", "zh": "答得不对？记一笔："},
    "ask_basis": {"en": "Nodes chosen", "zh": "选中的节点"},
    "ask_candidates": {"en": "Candidates", "zh": "候选"},
    "tier_help": {"en": "how the system decided this row's provenance", "zh": "系统如何判定该行的来源"},
    "declared": {"en": "declared", "zh": "声明式"},
    "declared_help": {"en": "not code — somebody put this in the graph in one sentence", "zh": "不是代码：有人用一句话把它放进图里"},
    "declared_lane": {"en": "Declared constraints", "zh": "声明式约束"},
    "declares": {"en": "declares", "zh": "声明"},
    "constrains": {"en": "constrains", "zh": "约束"},
    "level_help": {"en": "how checkable the source is", "zh": "来源的可核对程度"},
    # DP phase 4: the numbers that live in decks and reports
    "no_source": {"en": "no traceable data source", "zh": "无可追溯数据来源"},
    "no_source_help": {"en": "this number was typed in, not measured — nothing in the graph produces it",
                       "zh": "这个数字是人填进来的，不是算出来的——图里没有任何东西产出它"},
    "occurrences": {"en": "Occurrences", "zh": "出现位置"},
    "occurrences_help": {"en": "where this number turned up — a file is a place, not the node itself",
                         "zh": "这个数字出现过的地方——文件是位置，不是节点本身"},
    "no_occurrences": {"en": "no file is anchored to this node yet", "zh": "还没有文件锚定到这个节点"},
    "anchor_ok": {"en": "ok", "zh": "有效"},
    "anchor_lost": {"en": "anchor lost", "zh": "锚点失效"},
    "anchor_unchecked": {"en": "never checked", "zh": "未检查过"},
    "anchor_never_moved": {"en": "a lost anchor is reported, never re-pointed",
                           "zh": "失效的锚点只报告，绝不改指"},
    "seen_at": {"en": "seen", "zh": "看到时间"},
}

# One-line definitions for the tooltips. The tier and source-level labels are the
# ledger's own words, which is right for a reader who works with the ledger — but
# a label alone does not teach anyone what it means, so the definition rides along.
DEFS: dict[str, dict[str, dict[str, str]]] = {
    "tier": {
        "observed": {"en": "observed: computed from the analysis run", "zh": "系统观测：由分析批次算出"},
        "derived": {"en": "derived: inferred by a rule, not measured", "zh": "系统推导：由规则推出，非直接测量"},
        "asserted": {"en": "asserted: the agent's reading, recorded as such", "zh": "模型断言：agent 的判断，如实标注"},
        "stated": {"en": "stated: the user's own words, with the span", "zh": "用户陈述：用户原话，带出处区间"},
        "unstated": {"en": "unstated: nothing was recorded", "zh": "未陈述：当时没有记录"},
    },
    "evidence_level": {
        "linked": {"en": "linked: a reference you can open", "zh": "可核对链接：可打开的出处"},
        "verbal": {"en": "verbal: the words themselves", "zh": "原话记录：话本身"},
        "task_context": {"en": "task-context: inferred from the surrounding task", "zh": "仅任务上下文：由任务脉络推得"},
        "unstated": {"en": "unstated: nothing was recorded", "zh": "未陈述：当时没有记录"},
    },
}


def define(token, *, kind: str, lang: str = DEFAULT_LANG) -> str:
    """The one-line definition behind a label, for `title=`."""
    row = DEFS.get(kind, {}).get(str(token or ""))
    return (row.get(lang_of(lang)) or row[DEFAULT_LANG]) if row else ""


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
    """The page's phrase for one of the ledger's tokens. An empty token is an
    empty string; an unknown one comes back as itself (and `is_fallback` says so)."""
    if token in (None, ""):
        return ""
    row = WORDS.get(kind, {}).get(str(token))
    if not row:
        return str(token)
    return row.get(lang_of(lang)) or row[DEFAULT_LANG]


def ui(key: str, lang: str = DEFAULT_LANG, **fmt) -> str:
    """One of the page's own strings. An unknown key comes back as the key, which
    is ugly on purpose — an invisible missing string is the failure to avoid."""
    row = UI.get(key)
    if not row:
        return key
    text = row.get(lang_of(lang)) or row[DEFAULT_LANG]
    return text.format(**fmt) if fmt else text


# ── counts state a measurement ───────────────────────────────────────────────
# "read_hit: 0" is a fact nobody can act on, and "nobody looked at it!" is a
# story the ledger has no right to tell. These state the measurement and keep
# `shown` and `adopted` apart: the system can record that a record was SHOWN,
# never that anyone read it.

def shown(n: int | None, lang: str = DEFAULT_LANG) -> str:
    n = int(n or 0)
    if lang_of(lang) == "zh":
        return "未呈现" if n == 0 else f"呈现 {n} 次"
    return "Surfaced 0" if n == 0 else f"Surfaced {n}"


def adopted(n: int | None, lang: str = DEFAULT_LANG) -> str:
    n = int(n or 0)
    if lang_of(lang) == "zh":
        return "未被采纳" if n == 0 else f"被 {n} 个计划采纳"
    return "Adopted 0" if n == 0 else f"Adopted {n}"


def adopted_by(title: str, lang: str = DEFAULT_LANG) -> str:
    """One adopting plan, named."""
    if lang_of(lang) == "zh":
        return f"被计划《{title}》采纳"
    return f"Adopted by plan {title}"


def folded(n: int | None, lang: str = DEFAULT_LANG) -> str:
    n = int(n or 0)
    if lang_of(lang) == "zh":
        return f"另有 {n} 次无变更匹配"
    return f"Unchanged matches: {n}"
