# Agent 项目说明

## 文档职责

本文只维护当前代码结构、运行约束和长期有效的工程边界，不记录逐次迭代历史。

- 当前架构与工程规则：本文。
- 设计动机与 proof-system redesign：[`tmp/plan1.md`](tmp/plan1.md)。
- 阶段里程碑、完成状态和后续计划：[`docs/development-roadmap.md`](docs/development-roadmap.md)。
- 旧阶段的详细方案与实验记录：`tmp/phase*_plan.md`、`tmp/phase*_findings.md`，仅作历史参考。

如果历史计划与当前实现冲突，以代码、测试和本文记录的当前边界为准；同时修正文档，不把旧阶段描述继续复制到代码注释中。

## 运行规则

- Lean smoke / 真实 checker：需要时直接申请非沙箱运行，尤其是 elan toolchain 相关命令。
- 不读取 `.env` 内容，只检查存在性并让脚本消费，避免把 key 输出到日志。
- 不递归扫描 `lean_workspace/.lake/`、`.runs/`、缓存目录或第三方 Mathlib 源码。
- 搜索优先限定在 `agent/`、`tests/`、`docs/`、`scripts/` 和明确指定的 `tmp/` 文件。

## 项目目标

本项目是面向 Lean 4 的成本敏感证明搜索框架：

1. 接收自然语言数学问题或已有 Lean scaffold。
2. Formalizer 将自然语言转换为可检查的 Lean 任务；多洞输出拆成依赖任务链。
3. controller 在统一预算内生成候选、调用 Lean checker，并保留可追溯观测。
4. checker 接受后执行 statement-preservation / anti-cheating 安全审查。
5. structured 模式可组织 obligation DAG、证明分支、数学论证、Lean artifact 和失败证据。
6. 返回完整 accepted proof，或包含原始观测、成本和 partial/blocked 状态的失败结果。

最终实验目标与判定门槛见 [`docs/development-roadmap.md`](docs/development-roadmap.md)。

## 当前模块结构

```text
agent/
├── agents/
│   ├── chat_driver.py       # 通用 chat-completion 与 tool loop
│   ├── openai.py            # OpenAI-compatible transport / usage
│   ├── formalization.py     # 自然语言 -> Lean scaffold
│   ├── proof.py             # minimal proof generator
│   ├── structured.py        # typed structured proposal generator
│   ├── context.py           # retry 上下文压缩
│   └── tools/               # Lean 环境与 bounded scratch checker
├── search/
│   ├── controller/          # minimal ProofController
│   ├── structured/          # structured controller / frontier / reducer
│   │   ├── controller/
│   │   ├── proposal/
│   │   ├── reducer/
│   │   ├── projection/
│   │   ├── action_frontier.py
│   │   ├── cost_estimator.py
│   │   ├── budget_snapshot.py
│   │   └── model_router.py
│   ├── budget.py
│   ├── cost.py
│   ├── cost_ledger.py
│   ├── memory.py
│   ├── metrics.py
│   ├── safety.py
│   ├── execution.py
│   └── factory.py
├── proof_system/
│   ├── lean.py              # LeanAdapter 编排
│   ├── lean_*.py            # project / command / subprocess / feedback / server
│   ├── assembler.py         # multi-obligation final assembly
│   └── workspace/           # obligations / branches / actions / artifacts
├── tasks/                   # 单洞与多洞依赖任务构建
├── input/                   # 输入规范化和 scaffold 校验
├── retrieval/               # 词法 Lean 检索
├── runtime/                 # workspace / trace / logging / env loader
└── cli/                     # solve / formalize / prove
```

子模块包的 `__init__.py` 保留公共 API re-export。现有公共导入路径除非明确做兼容迁移，否则不要随内部拆分变化。

## 核心执行边界

### 单 Proof Agent 原则

证明修订保持单一 Proof Generator + controller。数学论证、Lean 实现与错误修复可以使用不同 prompt 或模型 tier，但不拆成彼此丢失上下文的多级 proof agent。Context Manager 只压缩上下文，不拥有分支和归因责任。

### Minimal 与 structured

- `ExecutionMode.MINIMAL`：线性生成—检查—反馈循环，携带紧凑 `ProofMemory`。
- `ExecutionMode.STRUCTURED`：使用 `ProofWorkspace`、obligation DAG、分支、typed action 和 final assembly。
- mode 在一次运行内不可变；`build_controller` 是唯一选择点。
- 两种模式不得静默退化为对方。
- minimal 不应 import structured 子包或承担其初始化成本。

### Workspace 与 reducer

- workspace 数据结构 frozen，并支持确定性序列化。
- obligation 依赖边从使用者指向所需前提；版本变化必须保留 superseded provenance。
- reducer 是 workspace 状态转移的权威入口，不直接 mutate 旧 workspace。
- Observation 是 append-only 证据；FailureHypothesis 是模型竞争假设，不承载基础设施错误。
- capability 缺失可以阻塞路线，但不能用于判定命题为假。

### Checker、safety 与 assembly

- `CheckResult.accepted` 是 Lean checker 的原始结果。
- `ControllerResult.accepted=True` 还要求 safety review 通过。
- safety review 检查 statement preservation、残留 `sorry` / `admit` 和新增 `axiom`。
- 多 obligation 只有在 active obligations 都有版本相容的 accepted artifact 后才能 final assembly。
- assembly 必须重新运行整体 checker 与 safety review。

### 多洞任务

- TaskBuilder 每个任务只暴露一个 active `{{proof}}`。
- 多洞 scaffold 按保守源码顺序生成依赖任务；后继任务使用显式 dependency marker。
- dependency marker 只能由 checker+safety accepted 的 proof materialize，不能使用 ground truth 或未验证候选。
- 同一 declaration 内多个洞必须由 Formalizer 拆成不同声明。
- CLI 自动顺序运行 TaskBuilder 产生的依赖任务链；显式 `--task-id` 可选择单个任务。

### 成本与预算

- model request、provider usage、tool、checker、pricing 和 charge 使用 append-only `CostLedger`。
- unavailable measurement 不得转换为零；observed、estimated、unavailable、unbounded 必须区分。
- token 口径区分 input、cached input、visible output、reasoning 和 billed/provider total。
- 价格只能来自显式冻结的价格表或 provider-reported charge，不能在代码中硬编码会过期的在线价格。
- 成本策略只改变调度和预算 admission，不改变 checker、safety、reducer 或 assembly 语义。

## Agent 角色

| 角色 | 类 | 职责 |
|---|---|---|
| Formalizer | `ChatFormalizationAgent` | 自然语言转 Lean scaffold，并可通过 checker 修订 |
| Proof Generator | `ChatActionGenerator` | minimal 模式候选 proof body |
| Structured Generator | `ChatStructuredActionGenerator` | typed structured action proposal |
| Context Manager | `ChatContextSummarizer` | 压缩 retry 反馈和历史上下文 |

`ChatDriver` 负责请求 payload、tool loop 和最终候选请求。新增模型角色应优先复用该驱动与已有 transport/tool 协议。

## 导入风格

- 同一子模块包内优先使用 1～2 个点的相对导入。
- 需要 3 个及以上点时使用绝对导入，避免 `from ....x import ...`。
- minimal 共享模块不得为了类型方便 import structured 实现。

## 关键公共协议

- `FormalizationAgent`：形式化入口。
- `ActionGenerator` / `StructuredActionGenerator`：候选生成入口。
- `ProofSystemAdapter` / `LeanAdapter`：checker 适配。
- `SafetyReviewer` / `StatementSafetyReviewer`：安全审查。
- `ProofMemory` / `MemoryProcessor`：minimal retry memory。
- `Retriever`：检索边界。
- `ScaffoldChecker`：形式化 scaffold 校验。

## CLI

```bash
python -m agent.cli solve --problem "Prove True"
python -m agent.cli formalize problem.md -o scaffold.json
python -m agent.cli prove scaffold.json -o proof.json
```

常用选项包括 `--execution-mode`、`--frontier-policy`、`--max-checks`、`--max-model-calls`、`--proof-model`、`--strong-proof-model`、`--enable-model-routing`、`--enable-retrieval` 和成本预算选项。

## 测试

```bash
python -m pytest -q
```

按改动范围优先运行相关测试：

- minimal：`test_controller.py`、`test_memory.py`、`test_safety.py`。
- structured：`test_structured_controller.py`、`test_reducer.py`、`test_frontier.py`。
- workspace/assembly：`test_workspace.py`、`test_assembler.py`、`test_solution_tracker.py`。
- model/tool：`test_chat_driver.py`、`test_model_adapter.py`、`test_tools.py`。
- task pipeline：`test_task_builder.py`、`test_input_normalizer.py`、`test_app_cli.py`。
- cost runtime：`test_cost_ledger.py`、`test_cost_estimator.py`、`test_unified_budget_snapshot.py`、`test_action_frontier.py`、`test_model_router.py`。
- benchmark harness：相应 benchmark script tests。

全量测试中的真实 Lean toolchain smoke 可能需要非沙箱执行；超时属于基础设施结果，不应伪装成证明失败。

## 注意事项

- `ChatConfig` 支持 OpenAI-compatible endpoint。
- typed generation failure 不得折叠成 `no_actions`。
- unknown cost 不能当作零或无穷大；排序和 admission 必须显式记录降级原因。
- benchmark 历史入口中的 `phase8_*` / `phase10_*` 文件名以及 cost snapshot 内的旧 `estimator_version` 是持久化兼容标识；除非提供迁移器，否则不要仅为清理命名而重命名。
