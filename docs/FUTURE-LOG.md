# FUTURE LOG

被推迟的工作项账本。每个提交代码的 PR 追加本 PR 推迟或新发现的项；完成时把状态改为 DONE 并注 PR 号，不删行。
状态：`DEFERRED`（有意推迟）· `WIP`（部分完成）· `DONE`。

| ID | 来源 PR | 状态 | 条目 | 归属阶段 |
|---|---|---|---|---|
| FL-001 | phase1 | DEFERRED | notebook `.ipynb` 支持 | 未定 |
| FL-002 | phase1 | DEFERRED | 拆分 / 合并的历史分叉（DAG）建模；现按断裂处理并标注 | 阶段 4 后 |
| FL-003 | phase1 | DEFERRED | 第四层身份：多签名分歧度打分 + LLM 仲裁（需先有校准数据） | 阶段 7 |
| FL-004 | phase1 | DONE（阶段 7 Task 3：`analyzer backfill <repo> --since SHA [--until] [--every] [--subdir] [--fresh-db]`，worktree 回放、trigger=backfill、拒绝真实历史除非 fresh、可续跑） | 历史 commit 回填命令（`analyzer backfill --since <sha>`） | 阶段 7 后 |
| FL-005 | phase1 | DEFERRED | `node_snapshot` 累积压缩策略 | 阶段 3 后 |
| FL-006 | phase6 | DONE | host API（NodeTypeProvider / provledger.testing）放 pip 包还是 skills/ | 阶段 6 前拍板 |
| FL-007 | phase1 | DONE（阶段 7 Task 1：migration 017 `metrics` + `record-metric` / `metrics_from_stdout` + `MetricChannel`，demo 的 +23.2% 成为 observed outcome） | eval metric outcome 通道（当前无表，demo 指标只在日志） | 阶段 7 |
| FL-008 | phase1 | DEFERRED | 与 superpowers 同名 skill 的长期方案（现为项目级禁用 + bootstrap 提示） | 阶段 5 |
| FL-009 | phase1 | DONE（阶段 8 Task 4：`GET /node/{project}/{qualified_name}`——上下游（consistency card）、按 run 分组的履历事件（plan 链接、tier 文字标签、`identity_asserted` 带 evidence）、理由与锚定约束（restricted 只给 why_ref）一次查询，页脚 approx_tokens；Reasons 面板的 node_key 变链接；outcome 视图见 FL-042） | dashboard 展示 node 历史与 outcome | 阶段 8 |
| FL-010 | phase1 | DEFERRED | 数据流签名依赖 dtype 覆盖率（unknown 偏多 → trivial 签名不参与匹配） | 持续 |
| FL-011 | #36 | DONE | dogfood：`deviate.sh` 在 api 返回 `accepted:false`（immutability 断路器）时仍打印 `ok:true` 并 exit 0；且 COMPLETED 步骤无任何脚本能插入重试子步骤 | 阶段 2 前 |
| FL-012 | #36 | DONE | dogfood：`ensure-dashboard.sh` 指向 `~/skill-workspace/orchestrator-webapp/launch_dashboard.sh`（不存在）；SKILL.md 调用路径仍为 `~/.code_puppy/skills/...` | 阶段 2 前 |
| FL-013 | #36 | DONE | dogfood：`selfcheck dtype_consistency_e2e` 在 prov_ledger 自身报 130 处误报（`_free_port:return: produced int != consumed dict \| None`），`init_project.sh` exit 1 但注册已写入 | 阶段 2 |
| FL-014 | phase3.5 | DONE | dogfood：NEEDS_REVIEW 触发只看 goal/step 文本的项目名 token 匹配，忽略 plan-input 的 `project` 字段；不含项目名的 project-scoped plan 会静默跳过 review | 阶段 2 前 |
| FL-015 | #36 | DONE | dogfood：分支 push 后 `review_diff.resolve_range` 选 remote 模式 `@{u}..HEAD`，upstream 就是本分支 → 空 diff、changed_symbols=[]，review 实际什么都没查；应对比注册 sha 或 merge-base | 阶段 2 前 |
| FL-016 | phase2 | DONE | 仓库根 `tests/test_skill_bundle.py` 依赖本地不入库的 `docs/superpowers/specs/2026-06-25-skill-diff.md`，fresh clone 必红 | 阶段 2 前 |
| FL-017 | phase3 | DONE | dogfood：`run-step.sh` 捕获的输出含无效 UTF-8（如 `cut -c` 切断多字节字符）时，`complete-step` 先把状态写成 COMPLETED、再在 `append_log` 抛 `UnicodeEncodeError: surrogates not allowed` 并 exit 1 —— 步骤已完成但 log_context 只剩 kickoff 横幅；应在捕获处 `errors="replace"` 且状态与日志同事务 | 阶段 2 前 |
| FL-018 | phase3 | DONE | dogfood：update-psg `signature` 闸门（`contract_diff.signature_contract`）把只新增带默认值关键字参数的函数判为 `signature_changed`，且按**裸名**找依赖方——`cli.run` 撞上仓库里 7 处 `subprocess.run` 调用者、`start_run` 撞上 4 个测试 helper；加性变更应视为兼容，依赖方应按限定名解析 | 阶段 3 |
| FL-019 | phase3 | DONE | dogfood：review 子步骤 `REVIEW.1` 一旦 fail-step，后续 deviation 子步骤 COMPLETED 也不会让 REVIEW/plan 恢复，`agent-review-close.sh` 拒绝（"expected NEEDS_REVIEW"）——修复后没有任何路径记录人工 PASS，plan 永远 FAILED（p2-t6、p2-t9 两例，后者 review 真抓到了 bug 并在 REVIEW.1.1–.8 修好） | 阶段 3 |
| FL-020 | phase3 | DONE | dogfood：`publish_plan.py` 的 impact_preflight 以读写方式打开项目图库，遇到 review 刷新（`init_project.sh`）持有的写锁直接 `database is locked` 崩溃，plan 未发布；应 `mode=ro` + busy timeout，或降级为"graph busy"的 impact_context | 阶段 3 |
| FL-021 | phase3.5 | DONE | dogfood：`publish_plan.py` / `_apply_op._open_db` 只在 `Plans` 表缺失时才跑迁移，新迁移（014/015）对已有的 `~/skill-workspace/orchestrator.db` 静默不生效，阶段 3 靠手动 `db.run_migrations`；`schema_version.migration_file` 已能幂等跳过，应在每次 open 时直接跑 | 阶段 4 前 |
| FL-022 | phase3.5 | DONE | dogfood：FL-019 修复只覆盖"REVIEW.1 失败后子树恢复"；plan 因**普通步骤未恢复**而直接 FAILED（review 步骤 FAILED 且无子步骤，如 `p3-t3`）时，事后在该步骤下补的恢复子步骤不会让 `review_and_complete` 重新评估——需要"review 无子且 FAILED、所有顶层失败步骤已恢复 → 重走判定"的路径 | 阶段 4 前 |
| FL-023 | phase4 | DONE | dogfood：update-psg `stale_references` 闸门只看图不看 diff——同一 diff 里删掉的函数连同它唯一的调用点一起删除时仍报 stale（`_ensure_impact_column`），review 被打成 FAILED；`review_diff.report` 应像 `signature_contract` 一样把调用方文件在 changed_files 里的情形降为 warning | 阶段 4 前 |
| FL-024 | phase4 | DONE | dogfood：`finish-plan.sh` 的"强制完成兜底"在 `review_and_complete` 返回 `ready=False` 时直接 `complete_plan`，包括 plan 刚进入 NEEDS_REVIEW 的情形（FL-022 重开 `p3-t3` 后 status=COMPLETED 而 review_state=awaiting_agent）；兜底必须排除 `needs_agent_review` | 阶段 4 前 |
| FL-025 | phase4 | DONE | 场景测试抓到：`ledger-add.sh`（`ledger_cli._connect`）裸 `sqlite3.connect` 打开 orchestrator 库、不跑迁移，在一个还没被任何脚本打开过的库上 `add` 死于 `no such table: LedgerEntries`；改为经 `orchestrator.db.open_db + run_migrations`（FL-021 的"每次打开都迁移"应覆盖每一条写路径） | 阶段 4 |
| FL-026 | phase4 | DEFERRED | 场景体系不覆盖 notebook 变更（FL-001 的 notebook 节点在 `pipeline_repo` fixture 里没有对应物）；需要一个带 `.ipynb` 的 fixture 和 `cell_changed`/`cell_moved` 一类事件的 expect 词汇 | 阶段 5 |
| FL-027 | phase4 | WIP（阶段 7 Task 2：`graph_api.Arbiter` + 校准导出 + `arbiter-eval` 门槛 + `cli.run` 按门槛接线已做，模型仍未接——见 FL-040/041） | 身份仲裁只有确定性 stub：`test_llm_consistency.py` 用 `PROVLEDGER_ARBITER=module:function` 注入真实模型并要求 N 次一致，但没有任何模型接入、没有把 `identity_asserted` 事件接进 `init_project.sh`（`history.resolve(arbitrate=...)` 仍是 None）；接入时要先过一致性门槛再写 asserted | 阶段 5 |
| FL-028 | phase5 | DONE | dogfood：review 刷新中途被杀（系统低内存杀掉 `review_run.py`）后，项目图库留下一行 0 事件的 `analysis_run`（run 32，plan `p4-t5`）和一个只重建了一部分的 node 表，直到下一次刷新才被 `reset_graph` 覆盖；`init_project.sh` 应把分析写进临时库再原子替换，或至少把中断的 run 标成 aborted，让 history/selfcheck 不把它当成一次真实观测。`review_run.py` 这侧已修：REVIEW.1 IN_PROGRESS 且注册 sha 落后 HEAD 时重做 1–4b 而不是跳到 4c | 阶段 5 |
| FL-029 | phase6 | DONE | dogfood：两个闸门按**裸名**连线——`stale_references` 对改名 `namesets.get → names` 报了 20 个 "still calls get"（仓库里所有 dict/requests 的 `.get()` 调用方，全在 diff 之外），`selfcheck.dtype_consistency_e2e` 把返回 `frozenset[str]` 的 `get` 当成所有 `.get()` 消费者的生产者（23 处断裂，刷新直接失败）；两处都应按限定名 / 已解析的边连线（PSG-C2 已给 profiles 做过），裸名只作降级并降为 warning。本阶段绕过：访问器改名 `names()` | 阶段 6 |
| FL-030 | phase6 | DONE | dogfood：FL-019 的递归恢复在恢复子步骤 COMPLETED 的一瞬间就把 plan 关成 reviewed——不检查注册 sha 是否等于 HEAD、不给填理由的机会（Task 2 的 28 个槽位全被系统兜底成 unstated，事后用 reason-fill 追加）。`_close_reviewed(reopened=True)` 应在注册 sha 落后 HEAD 时拒绝关闭（或自动跑 4b），并把 reason-slots 清单打进日志；另外 review_run 应能在 REVIEW.1 已 FAILED 时以恢复子步骤的身份重跑 1–4c | 阶段 6 |
| FL-031 | phase5 | DEFERRED | 扩展只读一份文件（三处取第一个存在的）：团队级 + 仓库级需要合并时没有办法；需要定义合并规则（同 id 冲突、priority 跨文件）后再开放多文件 | 阶段 6+ |
| FL-032 | phase5 | DEFERRED | `provledger-extensions.json` 只有 `version: 1`，没有 schema 迁移路径；`analysis_run.extensions_json` 记录的指纹形状也无版本号 | 阶段 6+ |
| FL-033 | phase5 | DEFERRED | `ledger_cli import` 只会新增/跳过，没有撤销：从扩展文件里删掉一条约束不会让已导入的条目失效（需要 `import --prune` 或 `revoke` 走 supersede） | 阶段 6+ |
| FL-034 | phase6 | DONE（阶段 7 Task 0：`review_diff.re_exported_symbols` 读 HEAD 源码，旧模块仍在模块级绑定该名（import/赋值）的命中降为 warning，`match=re_exported`；解析器本身仍不跟 import）| dogfood：解析器不跟 import——定义 git mv 到别处再由壳模块 re-export（`from x import y` / `globals().update`）后，未改动的调用方对旧限定名全部报 stale（Task 2 的 review 25 处 fail），只能用 `--accept-stale` 人判；`_resolve.Index` 应把 `from pkg.mod import name` 记成别名并按别名解析限定名 | 阶段 7 |
| FL-035 | phase6 | DEFERRED | provider 的 `schema_version` 只被记录（node_snapshot.attrs、extensions_json.providers），没有投影迁移钩子：同一 type_id 的 schema 升级后旧快照的 attrs 不会被转换，跨版本匹配也不做区分 | 阶段 7 |
| FL-036 | phase6 | DEFERRED | provider 自定义签名层（`x-…`）被 schema 接受但不参与匹配：`graph_api.match` 只认 qualname/struct/dataflow/owner 四层；要让第三方类型有自己的结构层，需要按层声明（是否 1:1、是否 trivial）并进 `_LAYERS` | 阶段 7 |
| FL-037 | phase6 | DEFERRED | 一致性套件对需要真图的第三方 provider（用 `ctx.node_rows`/`conn_ro` 读图的）只能在 PSG 侧注入 `context_factory` 跑；包内默认上下文是空图，`provledger.testing` 独立跑时这类 provider 的 `stability_matches_declaration` 只能覆盖纯源码 provider | 阶段 7 |
| FL-038 | phase7 | DEFERRED | `run_provider(isolate="subprocess")` 用 fork：子进程继承 ctx（含只读 sqlite 连接），观测 pickle 回传；没有 fork 的平台降级为 degraded，也没有内存/CPU 限额与 spawn 式干净环境——完整的子进程隔离（spawn + 按 `module:Class` 重新导入 provider + 重建上下文 + rlimit）留待需要时做 | 阶段 8+ |
| FL-039 | phase7 | DEFERRED | metric 通道没有聚合窗口：`MetricChannel` 取期望前后最近的一条观测（nearest-before/after），不做窗口均值；周期性指标的噪声会被当成后果 | 阶段 8+ |
| FL-040 | phase7 | WIP（阶段 8 Task 2：`provledger.testing.claude_arbiter.ClaudeArbiter`（无头 `claude -p`，注入 runner 的离线测试 14 个）已接到门槛前；对 67 条校准集 `--n-runs 3` 真实跑过一次：consistency 0.701 / coverage 0.433 / accuracy 0.552 / evidence_ok → **拒绝**（consistency < 1.0），未接入。主因是一致性：20 条在"给出正确 pairs"与"弃权"之间摇摆，201 次调用 0 次错连；16 条负例全部正确弃权，19 条 live 项 17 对。下一步：把"调用点同步改名"写成显式判据、让模型先列证据再判；门槛不放宽） | 没有任何真实模型仲裁器跑过门槛；`identity_asserted` 从未在真实图上出现过 | 阶段 8 |
| FL-041 | phase7 | DONE（阶段 8 Task 1：`calibration.generate_from_corpus / generate_negatives / generate_swaps / merge / stats`，`analyzer calibration generate|stats`，语料 swap 变体在 expect.toml 声明 truth 并由 harness 校验；48 条按构造：pairs 16 / none 16 / partial 16，struct 42 / dataflow 6） | 校准集一边倒：19 条全是 struct 层目录移动、truth 全 pairs、无负例——门槛拒绝说的是"样本考不出东西"而不是仲裁器的上限 | 阶段 8 |
| FL-042 | phase7 | DONE（阶段 8 Task 3：`GET /outcomes[?project=]`，`queries.get_expectations_with_latest_outcome` + `outcome_stats`） | dashboard 没有跨 plan 的 outcome 视图：只能在单个 plan 页看它自己的期望 | 阶段 8 |
| FL-043 | phase8 | DEFERRED | `ClaudeArbiter` 的原始 prompt / 回答只在进程内存（`exchanges`），`arbiter-eval` 不落盘，事后只能从报告的 answer / answers_distinct / correct 反推；需要 `--dump DIR` 把每次交换写下来，才能分析摇摆的具体证据 | 阶段 9 |
| FL-044 | phase8 | DEFERRED | 构造式校准集的 dataflow 层用的是包内的轻量替身（函数体里 `read_*()` 的字面量目标），不是分析器由图推出的 `dataflow_sig`；6 条 dataflow 项能让匹配器的 dataflow 层触发，但与真图的签名不是同一个函数——要么让 PSG 侧用 `analyzer_extractor` 重新生成这几条，要么在包里复刻真实签名 | 阶段 9 |
| FL-045 | phase8 | DEFERRED | 阶段 7 导出的 19 条 live 校准项没有 `source` 字段（`stats` 显示 `unknown=19`）；`--merge` 应给缺 source 的项补 `live:<db>`，或导出格式加版本迁移 | 阶段 9 |
| FL-046 | phase8 | DONE（DP 0/1 期 Task 7：`/node` 页与 `constraints.anchored_constraints` 都读 `change_reason(role=constraint)`，同一条规则、同一份数据；restricted 的 rationale 仍不出页面） | 节点履历页的约束查询在 `app/queries.py` 里直接写 SQL（`LedgerEntries` + `json_each`、restricted → rationale 置空），与 `orchestrator.constraints.anchored_constraints` 逻辑重复；webapp 只经 `provledger.psg_bridge` 读 PSG，但 orchestrator 侧的规则也该走一个入口 | 阶段 9 |
| FL-047 | phase8 | DEFERRED | consistency_card 的 `callers` / `output_consumers` 是裸名（`pkg.m.main`），节点页把它们当限定名链接，裸名会落到 "not in the state graph"；card 应记限定名（PSG-C2 给 profiles 做过同样的事） | 阶段 9 |
| FL-048 | dp-phase0/1 | DEFERRED | `LedgerEntries` 与 `node_reason` 仍在写（约束经 `ledger_store.add_entry` 双写、`insert_node_reason` 留给迁移与测试）；下一期停写旧表，`node_reason_v` / 视图只留一个版本（0.3.x）后删除 | DP 2 期 |
| FL-049 | dp-phase0/1 | DEFERRED | `read_hit` / `influence`（展示过 / 采用过）、plan headline、`why` 查询、PreToolUse 钩子、`because:[reason_id]`——spec §5/§6 的 2 期内容；本期 `reason-fill` 不接受 `because` | DP 2 期 |
| FL-050 | dp-phase0/1 | DEFERRED | `reason-fill` 对旧 `{node_key, text}` 形状退出 2 并说明；0.4 起连错误提示也删掉（直接按未知键处理） | 0.4 |
| FL-051 | dp-phase0/1 | DEFERRED | 钩子在会话启动时加载：安装本插件的那个会话本身不计 tool call、不记 utterance——本 PR 的 `docs/perf-baseline.json` 只有 66 个历史 plan 的代理量，measured 为空；下一个会话跑一次 `metrics baseline --write` 才有真实计数 | 下一会话 |
| FL-052 | dp-phase0/1 | DEFERRED | R2（响应 gate 失败）读的是上一个 plan 的 REVIEW 步骤日志里的 `[fail]` 行，不是 PSG 侧的结构化 gate 结果（`analysis_run.extensions_json` 不存 gate）；review_run 应把 gate 结果写成结构化记录供规则读 | DP 2 期 |
| FL-053 | dp-phase0/1 | DEFERRED | 构造 `change_reason` 时同一个约束按 subject 拆成多行（`anchored_constraints` 按 statement 去重）；spec 的 `change_reason` 只有单个 `node_key`，多主体约束需要 `reason_anchor` 一类的从表 | DP 2 期 |
| FL-054 | dp-phase0/1 | DEFERRED | 场景 golden 在本期改了两次（12 行 tier stated→asserted；constraint_bypassed 多一条 R5 derived 理由）——都是有意的语义变化，但"golden 不变"不再是本期的验收句；下一期应把 reasons 的 golden 段与事件流的 golden 段分开钉 | DP 2 期 |
| FL-055 | dp-phase0/1 | DEFERRED | `provledger note --ref` 用逗号分隔 key=value，label 里不能含逗号（本期 dogfood 第一次就撞上）；应改成可重复的 `--ref-kind/--ref-label/--ref-uri` 或用不常见的分隔符 | DP 2 期 |
