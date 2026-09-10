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
| FL-014 | phase1 | DEFERRED | dogfood：NEEDS_REVIEW 触发只看 goal/step 文本的项目名 token 匹配，忽略 plan-input 的 `project` 字段；不含项目名的 project-scoped plan 会静默跳过 review | 阶段 2 前 |
| FL-015 | #36 | DONE | dogfood：分支 push 后 `review_diff.resolve_range` 选 remote 模式 `@{u}..HEAD`，upstream 就是本分支 → 空 diff、changed_symbols=[]，review 实际什么都没查；应对比注册 sha 或 merge-base | 阶段 2 前 |
| FL-016 | #35 | DEFERRED | 仓库根 `tests/test_skill_bundle.py` 依赖本地不入库的 `docs/superpowers/specs/2026-06-25-skill-diff.md`，fresh clone 必红 | 阶段 2 前 |
| FL-017 | #35 | DEFERRED | dogfood：`run-step.sh` 捕获的输出含无效 UTF-8（如 `cut -c` 切断多字节字符）时，`complete-step` 先把状态写成 COMPLETED、再在 `append_log` 抛 `UnicodeEncodeError: surrogates not allowed` 并 exit 1 —— 步骤已完成但 log_context 只剩 kickoff 横幅；应在捕获处 `errors="replace"` 且状态与日志同事务 | 阶段 2 前 |
