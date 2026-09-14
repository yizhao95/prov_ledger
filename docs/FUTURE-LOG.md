# FUTURE LOG

被推迟的工作项账本。每个提交代码的 PR 追加本 PR 推迟或新发现的项；完成时把状态改为 DONE 并注 PR 号，不删行。
状态：`DEFERRED`（有意推迟）· `WIP`（部分完成）· `DONE`。

| ID | 来源 PR | 状态 | 条目 | 归属阶段 |
|---|---|---|---|---|
| FL-001 | phase1 | DEFERRED | notebook `.ipynb` 支持 | 未定 |
| FL-002 | phase1 | DEFERRED | 拆分 / 合并的历史分叉（DAG）建模；现按断裂处理并标注 | 阶段 4 后 |
| FL-003 | phase1 | DEFERRED | 第四层身份：多签名分歧度打分 + LLM 仲裁（需先有校准数据） | 阶段 7 |
| FL-004 | phase1 | DEFERRED | 历史 commit 回填命令（`analyzer backfill --since <sha>`） | 阶段 7 后 |
| FL-005 | phase1 | DEFERRED | `node_snapshot` 累积压缩策略 | 阶段 3 后 |
| FL-006 | phase1 | DEFERRED | host API（NodeTypeProvider / provledger.testing）放 pip 包还是 skills/ | 阶段 6 前拍板 |
| FL-007 | phase1 | DEFERRED | eval metric outcome 通道（当前无表，demo 指标只在日志） | 阶段 7 |
| FL-008 | phase1 | DEFERRED | 与 superpowers 同名 skill 的长期方案（现为项目级禁用 + bootstrap 提示） | 阶段 5 |
| FL-009 | phase1 | DEFERRED | dashboard 展示 node 历史与 outcome | 阶段 4 后 |
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
| FL-027 | phase4 | DEFERRED | 身份仲裁只有确定性 stub：`test_llm_consistency.py` 用 `PROVLEDGER_ARBITER=module:function` 注入真实模型并要求 N 次一致，但没有任何模型接入、没有把 `identity_asserted` 事件接进 `init_project.sh`（`history.resolve(arbitrate=...)` 仍是 None）；接入时要先过一致性门槛再写 asserted | 阶段 5 |
| FL-028 | phase4 | DEFERRED | dogfood：review 刷新中途被杀（系统低内存杀掉 `review_run.py`）后，项目图库留下一行 0 事件的 `analysis_run`（run 32，plan `p4-t5`）和一个只重建了一部分的 node 表，直到下一次刷新才被 `reset_graph` 覆盖；`init_project.sh` 应把分析写进临时库再原子替换，或至少把中断的 run 标成 aborted，让 history/selfcheck 不把它当成一次真实观测。`review_run.py` 这侧已修：REVIEW.1 IN_PROGRESS 且注册 sha 落后 HEAD 时重做 1–4b 而不是跳到 4c | 阶段 5 |
