# CLAUDE.md — 给 Claude Code 的工作导航

本文件给 Claude Code 会话用。**架构详情看 [agents.md](agents.md)**，本文件只讲导航、禁读边界、阶段路线和工作纪律——这些是控制上下文与 token 消耗的关键。

## 一、永远不要读 / 不要扫的目录

这些目录体积巨大或为生成产物，读进来会瞬间占满上下文：

| 目录 | 为什么禁 | 备注 |
|---|---|---|
| `lean_workspace/` | **14.5 万文件**（含 mathlib 与 `.lake` 构建产物） | 绝对禁止任何 `find`/`grep`/`Glob`/`Read` 递归进它。若要改 Lean 工程文件，先问用户具体路径。 |
| `lean_workspace/.lake/` | Lake 构建缓存 | 禁读禁改。 |
| `lean_workspace/.lake/packages/mathlib/` | 第三方 mathlib 源码 | 禁读。 |
| `.runs/` | 运行 trace 产物 | 默认禁读；只在用户要分析某次运行时按需打开**指定文件**。 |
| `__pycache__/`、`.pytest_cache/`、`*.pyc` | 字节码/缓存 | 禁读。 |
| `.env` | 含密钥 | **从不读取内容**，只检查存在性，由脚本消费（见 agents.md）。 |
| `.git/` | 版本元数据 | 禁读。 |

核心源码只在这几个目录里，**搜索范围应限定于此**：`agent/`、`tests/`、`docs/`、`scripts/`、`tmp/`（规划文档）、`app.py`。

需要大面积搜索时，用 Explore subagent（结论返回，原文不进主上下文），而不是自己 `grep`/`Read` 整个目录。

## 二、阶段路线图（proof-system-redesign）

完整设计见 [tmp/plan1.md](tmp/plan1.md)，**每个 Phase 的落地细节、改动文件、复用点、不变项、对应测试全在 [agents.md](agents.md)** —— 本节只给一行里程碑，便于快速定位当前进度，不重复 agents.md 的内容。当前分支 `feature/proof-system-redesign`。

> 阅读约定：改 structured 相关代码前，先看 [agents.md](agents.md) 对应 Phase 段的「不变项」（明确列出哪些模块该 phase 不动、minimal 路径不 import），这是避免越界改动和误判 minimal 成本的关键。

- **Phase 0** ✅：固定 baseline 与评测口径（`agent/search/metrics.py`、trace、fixtures）。
- **Phase 1** ✅：Minimal Refinement Core——结构化 goal state、self-managed `ProofMemory`、确定性 `StatementSafetyReviewer`、memory/goal_state 进 trace。
- **Phase 2** ✅：`ExecutionMode.MINIMAL/STRUCTURED` 参数 + `--execution-mode` CLI + 共同观测。**禁止运行时自动切换模式。**
- **Phase 3** ✅：`ProofWorkspace`/`ObligationGraph`/`VerifiedFact` + DAG 校验/版本规则 + `ArtifactAssembler` 整体复检；minimal 不 import workspace 包，零成本。
- **Phase 4** ✅：`ProofBranch`/`ArgumentStep`/`AlignmentLink`/`Observation` 数学论证层原语 + 确定性 checker→Observation 提取器。
- **Phase 5** ✅：`SearchAction`（`allowed_mutations` 作用域校验）+ `FailureHypothesis`（6 类竞争性语义假设）纳入权威 workspace。
- **Phase 6** ✅：`StructuredController` + frontier（确定性 tuple 排序）+ reducer（纯函数转移）+ AND-OR 骨架；factory 解锁 `STRUCTURED`。
- **Phase 7.0** ✅：端到端契约冻结——`build_result_summary`（frozen `ResultSummary`）+ `assembly_outcome`/`result_summary` 透传。
- **Phase 7.1** ✅：workspace context projection（`build_context_projection`）成为 prompt/summarizer 单一数据源。
- **Phase 7.2** ✅：typed `StructuredActionProposal` + `adapt_legacy_generator`；第一轮只执行 IMPLEMENT/REPAIR，DECOMPOSE/CAPABILITY 仅类型+序列化。
- **Phase 7.3** ✅：capability audit 真正执行 + obligation BLOCKED 同步（`_apply_capability_audit` + `_block_obligation`）。
- **Phase 7.4** ✅：`apply_decompose` 执行器 + frontier readiness gate + artifact kind（root PROOF_BODY / helper DECLARATION）+ 多义务 assembly。
- **Phase 7.5** ✅：helper 复用语义清理（`declaration_id` 按名引用 helper）+ `no_ready_work`→`stop_reason="blocked"`。
- **Phase 7.6** ✅：`PROPOSE/REFINE_ARGUMENT` + `CHANGE_REPRESENTATION` 执行 + 竞争性 `FailureHypothesis` 层（`_select_test_action` 低成本优先）。
- **Phase 7.7** ✅：`WorkspaceStatus.PARTIAL` + 终态 finalizer + 传递性 BLOCKED 传播 + evidence-driven DORMANT 恢复。
- **Phase 7.8** ✅：真实复杂任务 + minimal-vs-structured 消融对比（依赖真实 Lean + 真实 model，手动跑、不进 CI）。方案见 [tmp/phase7_8_plan.md](tmp/phase7_8_plan.md)，不引入新代码模块。
- **Phase 8** ⬜ **进行中**：成本感知搜索。8.0 ✅ `CostVector` 观测；8.1 ✅ branch/obligation 成本归因；8.2 ✅ opt-in cost-aware frontier（默认 legacy 排序不变，`--frontier-policy cost_aware_v1` opt-in，readiness 仍是 gate，`branch_id` 最终 tie-breaker）；8.3 ✅ 软预算与借用（`cost_aware_v2`）；8.4 ✅ value-per-cost 混合评分（`value_per_cost_v1` + `PriorityExplanation`，全整数 tuple）；8.5 ⬜ 真实任务消融。方案见 [tmp/phase8_plan.md](tmp/phase8_plan.md)，成本策略只改 structured 调度，不改证明语义。

## 三、工作纪律（控制 review-fix 与 token）

1. **每单个小任务一次提交**，提交粒度在 plan 阶段就定死（参考 Phase 1 的 6 个提交）。
2. **测试是 gate，不是讨论**：每个小任务只跑该任务相关的 2-3 个测试文件；全量 `python -m pytest tests/ -q` 只在每个 Phase 结束时跑一次。
3. **review/fix 是每个 Phase 一次**，不是每个 commit 一次。不要每提交就自动进入 `/code-review` 循环。
4. **计划阶段先用 EnterPlanMode** 把任务边界、改动文件、复用点、验证方式写清楚再动手，避免边做边返工。
5. 改 Lean 工程相关的东西需要真实 toolchain 时，申请非沙箱运行（见 agents.md「运行规则」）。
6. 导入风格：子包内优先使用 1~2 个点的相对导入；当相对导入需要 3 个点及以上时，必须使用绝对导入，避免 `from ....x import ...` 这种过深路径。

## 四、提交信息

提交信息形如 `Phase N: <小任务>`，结尾加：

```
Co-Authored-By: Claude <noreply@anthropic.com>
```
