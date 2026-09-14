# 重构变异语料库

每个 case：`cases/<name>/base/`（基准 repo）· `expect.toml` · `variants/<m>/`（手写变体 repo；含 `before/` `after/` 时以 before 为基准）。

被测对象是**身份匹配器**（analyzer.history.match）。harness 只依赖 `NodeObs`：
(node_type, qualified_name, file_path, struct_sig, dataflow_sig, dataflow_trivial)。

expect.toml：
- `[case] symbols`：基准里参与身份的符号限定名（始终写**基准侧**限定名，harness 按 `Pair.prev.qualified_name` 对账）。
- `[generated]` / `[variants]`：名字 → { identity = preserved|broken|ambiguous,
  semantic_diff = none|callers|expected, broken = [...], added = [...], via = qualname|struct_sig|dataflow_sig,
  symbols = [...]（可选，覆盖本变体参与身份的符号） }。
`via` 对该变体 symbols 集合里的**每个**符号生效，所以只有限定名变化的符号才应留在集合里（用 `symbols = [...]` 收窄）。
内建 must_not：preserved ⇒ 无 node_removed / identity_ambiguous；none ⇒ 无 node_changed。
预期失败用例（swap_two_similar → ambiguous）必须存在：锁定已知边界。

`generated` 变体由 `mutate.GENERATED` 里的 stdlib 变异器在 base 的临时副本上生成；
`variants` 变体是手写目录。harness 对 base 做两次提取并比对，提取器不确定即报 `determinism`。

## 结构签名（struct_sig）规则

语料库对"什么算同一个节点"的期望依赖 spec §2.3 里 `struct_sig` 的两条规则（阶段 1 review 定，spec 已同步）：

- 形参标注与返回标注不参与签名（类型名会随类改名而变；类型信息由数据流签名承担）。
- 类节点递归 α-归一化嵌套方法的形参与局部名。

`class_methods/rename_class` 变体依赖这两条：它把 `Trainer` 改名为 `Estimator`，连带改了 `fit` 的字符串返回标注 `"Trainer"→"Estimator"`。
按这两条规则 `fit` 的结构签名不变，所以期望是 `preserved` + `semantic_diff = "none"`（via `struct_sig`）；语料**不改**。
