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

完整设计见 [tmp/plan1.md](tmp/plan1.md)。当前分支 `feature/proof-system-redesign`。

- **Phase 0** ✅ 已完成：固定 baseline 与评测口径（`agent/search/metrics.py`、trace、fixtures）。
- **Phase 1** ✅ 已完成：强化 Minimal Refinement Core
  - 结构化 goal state：`agent/proof_system/base.py:GoalState`、`agent/proof_system/lean_feedback.py:extract_goal_states`
  - self-managed memory：`agent/search/memory.py`（`ProofMemory` / `MemoryProcessor`）
  - controller + prompt 接入 memory：`agent/search/controller.py`、`agent/agents/proof.py`
  - trace 记录 memory/goal_state：`agent/search/metrics.py`、`agent/runtime/trace_store.py`
  - SafetyReviewer：`agent/search/safety.py`（确定性 statement-preservation + anti-cheating）
- **Phase 2** ✅ 已完成：执行模式参数 `ExecutionMode.MINIMAL/STRUCTURED` + `--execution-mode` CLI + 共同观测。**明确禁止运行时自动切换模式。**
- **Phase 3** ✅ 已完成：ProofWorkspace 与 Obligation DAG
  - 结构化状态原语：`agent/proof_system/workspace.py`（`ProofObligation`/`ObligationGraph`/`ProofWorkspace`/`FormalSpecification`/`VerifiedFact`，frozen + 序列化）
  - DAG 合法性、版本规则（`new_version`/SUPERSEDED）、`initialize_from_task`、`decompose`、`register_accepted_fact`
  - final-assembly 整体复检：`agent/proof_system/assembler.py:ArtifactAssembler`
  - trace 集成：`workspace_payload` 透传 `metadata["workspace"]`；minimal 零成本
  - `build_controller` 对 STRUCTURED 仍抛错（frontier/AND-OR 是 Phase 6）
- **Phase 4** ✅ 已完成：ProofBranch / ArgumentStep / Alignment / Observation
  - 数学论证层原语在 `agent/proof_system/workspace/` 子包：`argument.py`（`ArgumentStep`/`ArgumentGraph`）、`artifact.py`（`LeanArtifact` 从 `assembler.py` 迁入并扩展）、`alignment.py`（`AlignmentLink`/`AlignmentRelation`）、`observation.py`（`Observation`/`ObservationSource` + 确定性 checker 提取器）、`branch.py`（`ProofBranch`/`BranchStatus`）
  - `ArgumentGraph.validate()` 确定性 DAG 校验；`observations_from_check_result` 把非 accepted 检查结果转成中立 Observation；无法对齐显式记 `UNALIGNED`
  - `ProofWorkspace` 接入 `branches: tuple[ProofBranch, ...]` + 序列化；`base.py:ProgressSignal` 补 `to_dict`/`from_dict`
  - `build_controller` 对 STRUCTURED 仍抛错（动作协议是 Phase 5、frontier 是 Phase 6）；minimal 路径不 import workspace 包
- **Phase 5** ✅ 已完成：统一 ProofAgent 动作与失败假设
  - 动作协议原语在 `agent/proof_system/workspace/` 子包：`action.py`（`SearchAction`/`SearchActionKind`/`MutationKind` + `DEFAULT_ALLOWED_MUTATIONS` 默认作用域表）、`hypothesis.py`（`FailureHypothesis`/`FailureKind`），frozen + 序列化。
  - 每个动作显式声明 `allowed_mutations`；只读默认作用域允许 argument/implement 同步维护 alignment；`SearchAction.validate()` 确定性校验 target_branch_id、rationale、allowed_mutations（允许 narrow、禁止 broaden，跨界须另起新动作）、target_step_ids 唯一性，返回 `SearchActionReport` 不抛。
  - `FailureHypothesis` 承载多个竞争性失败假设；`ProofBranch.last_action` / `failure_hypotheses` 将动作和假设纳入权威 workspace 与 trace，并校验 evidence/step/branch 引用；`FailureKind` 仅含 6 个模型竞争语义类别（不含基础设施错误）。
  - `build_controller` 对 STRUCTURED 仍抛错（frontier/AND-OR driver 是 Phase 6）；minimal 路径不 import workspace 包。
- **Phase 6** ✅ 已完成：Frontier 与 AND-OR 搜索（结构化执行器落地）
  - 结构化执行器在 `agent/search/structured/` 子包（与 `controller/` 平行、互不 import）：`frontier.py`（`Frontier`/`FrontierNode`，可变调度器只读 workspace、确定性 tuple 排序、`_stalled_streak` 纯函数派生）、`solution_tracker.py`（`has_complete_solution`/`select_solution` 纯函数，与 assembler 前置条件对齐）、`reducer.py`（`apply` 纯函数，绝不 mutate；accepted→ACCEPTED+register_accepted_fact，rejected→追加 observation 保留 artifact provenance，stall 阈值→DORMANT，根策略连续失败→派生 REPAIR 子 branch）、`run_state.py`（`_StructuredRunState` + `build_structured_result`，平行 `results.py`、复用 `summarize_run`）、`controller.py`（`StructuredController`，与 `ProofController` 同构造签名）
  - `StructuredController.run` 跑 plan1.md §12 的 structured 循环：`frontier.pop → 确定性选 IMPLEMENT/REPAIR_IMPLEMENTATION`（`DEFAULT_ALLOWED_MUTATIONS` 包装，复用现有 `ActionGenerator` 产出 proof body，不引入新模型协议）`→ render+check（复用 AttemptWorkspace/adapter.check）→ safety（仅 accepted）→ reducer.apply → frontier.update → has_complete_solution → assemble 终检`
  - 复用共享预算/metrics/trace 出口：每个 attempt=1 model_call+1 check，assemble 额外 reserve 1 check；`metadata["workspace"]` 透传序列化 workspace；minimal 路径零成本（factory lazy import，minimal 不 import structured 包）
  - `factory.build_controller` 解锁 `STRUCTURED` 返回 `StructuredController`；`StructuredModeUnavailableError` 类保留（向后兼容 import + CLI 防御性 except + 未知 mode 兜底）；mode-mismatch 检查对两分支都跑
  - 第一版只驱动单 root obligation（OR 搜索 + 分支状态机），retriever/context_summarizer 已接入但仍是 minimal 风格摘要（不是 plan1 §10 的 workspace 投影），不 decompose、不自动恢复 DORMANT——structured 上下文投影、DECOMPOSE、能力审计是 Phase 7

## 三、工作纪律（控制 review-fix 与 token）

1. **每单个小任务一次提交**，提交粒度在 plan 阶段就定死（参考 Phase 1 的 6 个提交）。
2. **测试是 gate，不是讨论**：每个小任务只跑该任务相关的 2-3 个测试文件；全量 `python -m pytest tests/ -q` 只在每个 Phase 结束时跑一次。
3. **review/fix 是每个 Phase 一次**，不是每个 commit 一次。不要每提交就自动进入 `/code-review` 循环。
4. **计划阶段先用 EnterPlanMode** 把任务边界、改动文件、复用点、验证方式写清楚再动手，避免边做边返工。
5. 改 Lean 工程相关的东西需要真实 toolchain 时，申请非沙箱运行（见 agents.md「运行规则」）。

## 四、提交信息

提交信息形如 `Phase N: <小任务>`，结尾加：

```
Co-Authored-By: Claude <noreply@anthropic.com>
```
