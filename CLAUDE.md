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
- **Phase 2**（下一步）：执行模式参数 `ExecutionMode.MINIMAL/STRUCTURED` + `--execution-mode` CLI + 共同观测。**明确禁止运行时自动切换模式。**
- Phase 3+：ProofWorkspace / Obligation DAG / ProofBranch / Frontier / AND-OR 搜索。见 plan1.md。

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
