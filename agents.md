# Agent 项目说明

## 设计依据

完整的 proof-system redesign 设计与分阶段计划见 [`tmp/plan1.md`](tmp/plan1.md)。本文只维护当前代码结构、运行约束和已经落地的阶段能力；涉及以下事项时，应先阅读该计划对应章节：

- Minimal Refinement Core 与 structured `ProofWorkspace` 的职责边界；
- 单 Agent 原则，以及数学论证、Lean 验证和错误归因不能被机械割裂的原因；
- `minimal` / `structured` 执行模式的参数化选择；
- 搜索树、预算感知策略和后续 Phase 的范围。

实现应以当前 Phase 的验收边界为准，不提前引入后续 Phase 组件。若本文的简述与计划冲突，以 `tmp/plan1.md` 的最新设计决策为准，并同步修正文档。

## 运行规则

- Lean smoke / 真实 checker：需要时直接申请非沙箱运行，尤其是 elan toolchain 相关命令。
- 不读取 `.env` 内容，只检查存在性和用脚本消费它，避免把 key 打出来。

## 项目目标

本项目是一个面向 Lean 4 的成本敏感证明搜索（cost-sensitive proof search）agent 框架。核心流程：

1. 接收自然语言数学问题或已有 Lean 模板。
2. **Formalizer** 把自然语言形式化为带有一个证明洞（hole marker）的 Lean scaffold。
3. **ProofController** 在预算限制下循环生成候选证明、调用 Lean checker，并通过紧凑 `ProofMemory` 携带修订上下文。
4. checker 接受后执行确定性的 statement-preservation / anti-cheating 安全审查。
5. 返回通过 checker 与安全审查的完整证明，或带原始观测和最终 memory 的失败报告。

## 模块结构

```
agent/
├── agents/              # 各 agent 角色与共享模型基础设施
│   ├── chat_driver.py   # 通用 chat-completion 驱动（payload、tool loop）
│   ├── openai.py        # ChatConfig / ChatTransport / OpenAI 兼容 HTTP 层
│   ├── tools/           # Tool 协议与 Lean 环境工具（__init__ 向后兼容 re-export）
│   │   ├── base.py      # Tool / FunctionTool / ToolCall / extract_tool_calls
│   │   ├── lean_env.py  # LeanEnvironmentToolProvider / extract_missing_imports
│   │   ├── lean_proof.py# LeanProofToolProvider（bounded scratch checker）
│   │   └── loop.py      # run_tool_loop
│   ├── formalization.py # Formalizer：自然语言 -> Lean scaffold
│   ├── proof.py         # Proof generator：补全 proof hole
│   ├── context.py       # ContextSummarizer：压缩反馈与历史上下文
│   └── config.py        # AgentRole / RoleModelConfig
├── search/              # 搜索控制
│   ├── action.py        # ActionGenerator 协议与 ActionCandidate
│   ├── controller/      # ProofController 主循环（__init__ 向后兼容 re-export）
│   │   ├── core.py      # ProofController
│   │   ├── types.py     # ControllerConfig / AttemptRecord / ControllerResult
│   │   ├── results.py   # 结果构造与 Phase 0 metrics
│   │   ├── context.py   # 检索与上下文摘要
│   │   └── utils.py     # _proof_phase / _edit_with_controller_metadata
│   ├── execution.py     # ExecutionMode 参数（minimal / structured）
│   ├── factory.py       # 按执行模式构造 controller 的唯一选择点
│   ├── budget.py        # 预算管理（checks / model calls / time）
│   ├── memory.py        # self-managed ProofMemory 与确定性更新器
│   ├── safety.py        # statement-preservation / anti-cheating 审查
│   ├── metrics.py       # Phase 0 原始 attempt/run 观测
│   ├── proposer.py      # 候选库生成
│   └── state_encoder.py # 证明状态编码
├── proof_system/        # Lean 适配层
│   ├── lean.py          # LeanAdapter 编排器
│   ├── lean_project.py  # Lake 项目检测
│   ├── lean_command.py  # lean / lake 命令行构建
│   ├── lean_subprocess.py # 子进程执行与进程树清理
│   ├── lean_feedback.py # Lean 诊断输出解析
│   ├── lean_server.py   # 持久化 Lean server
│   ├── workspace/       # Phase 3-5 ProofWorkspace / ObligationGraph / 分支 / 动作协议等纯数据
│   │   ├── obligation.py# ProofObligation / ObligationStatus
│   │   ├── graph.py     # ObligationGraph / ObligationGraphReport / DAG 校验
│   │   ├── spec.py      # FormalSpecification / VerifiedFact / WorkspaceStatus
│   │   ├── workspace.py # ProofWorkspace / initialize_from_task / branches 接入
│   │   ├── argument.py  # Phase 4 ArgumentStep / ArgumentGraph
│   │   ├── artifact.py  # Phase 4 LeanArtifact（从 assembler.py 迁入并扩展）
│   │   ├── alignment.py # Phase 4 AlignmentLink / AlignmentRelation
│   │   ├── observation.py# Phase 4 Observation / ObservationSource / checker 提取器
│   │   ├── branch.py    # Phase 4 ProofBranch / BranchStatus
│   │   ├── action.py    # Phase 5 SearchAction / SearchActionKind / MutationKind / 作用域校验
│   │   └── hypothesis.py# Phase 5 FailureHypothesis / FailureKind
│   ├── assembler.py     # Phase 3 final-assembly 整体复检
│   └── base.py          # ProofSystemAdapter / ProofTask / CheckResult / ProgressSignal
├── retrieval/           # 检索（当前为词法检索 LexicalLeanRetriever）
├── input/               # 输入解析、规范化、scaffold 校验
├── tasks/               # 任务构建
├── runtime/             # workspace、trace store、logging、env loader
└── cli/                 # 命令行入口 app.py
```

> `controller`、`workspace`、`tools` 已拆分为子模块包；每个包的 `__init__.py` 通过向后兼容的 re-export 保留原有公共 API，现有 `from agent.search.controller import ...`、`from agent.proof_system.workspace import ...`、`from agent.agents import ...` 等导入路径无需修改。

## Agent 角色

| 角色 | 类 | 输入 | 输出 | 说明 |
|---|---|---|---|---|
| Formalizer | `ChatFormalizationAgent` | `FormalizationRequest` | `FormalizationResult` | 生成含 `{{proof}}` 的 Lean scaffold，可接 checker 做校验重试。 |
| Proof Generator | `ChatActionGenerator` | `ActionGenerationRequest` | `Sequence[ActionCandidate]` | 根据当前 proof state 生成候选证明体。 |
| Context Manager | `ChatContextSummarizer` | `SummarizationRequest` | `SummarizationResult` | 在 retry 前把 checker 输出与历史反馈压缩成简短、可操作的摘要，降低 proof generator 的 prompt 成本。 |

当前证明修订保持单一 Proof Generator + `ProofController`，不为数学推理、形式化和错误修复继续拆分多级 proof agent。`Context Manager` 是可选的上下文压缩器，不拥有证明分支或错误归因责任。

## Phase 1 Minimal Refinement Core

- `lean_feedback.py` 同时保留旧的 `unsolved_goals`，并提取带 fingerprint、source span、declaration id、`is_sorry_goal` 的结构化 `GoalState`。
- `ProofMemory` 是跨 retry 携带的主要紧凑上下文，记录 checker 支持的事实、失败方法、Lean API 经验、开放目标和来源 attempt id；各字段有上限，避免 prompt 无界增长。
- `MemoryProcessor` 是确定性组件。只有 checker 接受且安全审查通过的结果才能进入 `established_facts`。
- `StatementSafetyReviewer` 只在 checker 接受后运行，检查固定 statement 前缀、残留 `sorry` / `admit` 和新增 `axiom`；注释及字符串不参与关键字扫描。
- 安全审查拒绝不改写 checker 的原始结果：attempt trace 仍记录 checker 观测，controller 的最终结果保持未接受，并继续下一次修订。
- trace 保留每次 attempt 的原始分类、goal fingerprints、完整结构化 `goal_state`，并在 run summary 中记录最终 memory 与安全拒绝原因。
- 当前仍是线性 minimal loop。搜索树、预算感知分支策略以及 structured `ProofWorkspace` 属于后续阶段，不在 Phase 1 内推断或自动启用。

## Phase 2 执行模式参数与共同观测

- `ExecutionMode`（`minimal` / `structured`）由启动参数决定，同一次运行内不可变；`build_controller`（`search/factory.py`）是唯一的选择点。
- CLI 通过 `--execution-mode`（默认 `minimal`）选择模式，作用于 `solve` 与 `prove`。
- 当前仅实现 `minimal` 执行器（`ProofController`）。选 `structured` 时 `build_controller` 抛 `StructuredModeUnavailableError`，CLI 以 `stage=execution_mode` 返回非零退出码，**不**静默退化为 minimal，避免把 structured 运行误记成 minimal。
- `execution_mode` 作为共同观测字段记录在 `RunMetrics`，经 `_metrics_payload` 进入 `run_summary.metrics`，便于跨模式公平比较。
- 共同观测层只记录原始事实（attempt、checker category、goal fingerprints、耗时、预算、execution_mode），不推导 progress、stall 或跨运行统计。
- 运行中不存在任何切换模式的代码路径：`ControllerConfig` 是 frozen、`_ControllerRunState` 不持有 mode、`ProofController` 在本阶段不读 mode 做控制流。

## Phase 3 ProofWorkspace 与 Obligation DAG

- 结构化状态原语集中在 `agent/proof_system/workspace/` 包（与 `ProofTask`/`CheckResult` 同层，proof-system-neutral 纯数据）：`ProofObligation`、`ObligationGraph`、`ProofWorkspace`、`FormalSpecification`、`VerifiedFact`，全部 frozen + `to_dict`/`from_dict`。
- `ObligationGraph.validate()` 确定性检查无环、依赖存在、活跃依赖不指向 SUPERSEDED 版本、根存在且根的证明依赖闭包覆盖所有活跃义务；`dependency_ids` 从使用者指向其所需前提（root → helper）；返回 `ObligationGraphReport`，不抛异常。
- 版本规则：statement/assumption/dependency 变化走 `ObligationGraph.new_version`，旧实例置 SUPERSEDED 并保留作 provenance；`by_id` 只解析到最新非 SUPERSEDED 版本。
- `initialize_from_task` 从 `ProofTask` 造单 root obligation 的合法工作区，是 structured 模式入口；`decompose` 加辅助义务并创建依赖这些子义务的新 parent 版本；`register_accepted_fact` 只接受 checker 与 safety 均通过的证据，把 obligation 置 ACCEPTED 并记录带 obligation version 的 `VerifiedFact`。
- `agent/proof_system/assembler.py` 的 `ArtifactAssembler.assemble` 在所有活跃 obligation 均 ACCEPTED 且 artifact id/version 匹配时，按依赖序拼装、整体复检并执行 safety review；前置失败返回 blocked `AssemblyResult`，不抛。
- 序列化产物经 `ControllerResult.metadata["workspace"]` 进入 trace，`trace_store.workspace_payload` 透传；minimal 运行不写该键，`ProofController` 不 import workspace 模块，因此 minimal 不承担 DAG 成本。
- 本阶段只交付数据结构 + 序列化 + trace + DAG 规则 + final-assembly 复检 + root 初始化。`build_controller` 对 `STRUCTURED` **仍抛** `StructuredModeUnavailableError`：驱动这些状态的 frontier / AND-OR 搜索是 Phase 6，未提前引入。

## Phase 4 ProofBranch / ArgumentStep / Alignment / Observation

- 数学论证层原语补齐在 `agent/proof_system/workspace/` 子包：`ArgumentStep`/`ArgumentGraph`（`argument.py`）、`LeanArtifact`（`artifact.py`，从 `assembler.py` 迁入并新增 `declaration_id`/`source_span`/`proof_body`）、`AlignmentLink`/`AlignmentRelation`（`alignment.py`）、`Observation`/`ObservationSource`（`observation.py`）、`ProofBranch`/`BranchStatus`（`branch.py`），全部 frozen + `to_dict`/`from_dict`。
- `ArgumentGraph.validate()` 确定性校验 step id 唯一、`depends_on` 引用存在、依赖边无环，复用 Phase 3 `graph.py` 的 DFS 三色标记；返回 `ArgumentGraphReport`，不抛。
- `AlignmentRelation` 三态：`IMPLEMENTS`/`PARTIAL`/`UNALIGNED`，无法精确对齐时显式记 `UNALIGNED`，不伪装确定归因。
- `observations_from_check_result(check_result, attempt_index)` 是确定性提取器：非 accepted 的 `CheckResult` 按 `parsed_feedback.goal_state` 每个 goal 生成一条 `Observation`（`raw_evidence_ref="attempt:<N>"`），无 goal 时回退单条 summary；accepted 返回空 tuple。`category` 存为 str 以兼容未来新增分类。
- `ProofBranch` 串起 `argument`/`lean_artifact`/`alignment`/`observations`/`progress`/`status`，`obligation_id`+`obligation_version` 锚定到具体义务版本；`parent_branch_id` 表示修复/策略切换派生的子分支。`progress` 复用 `base.py:ProgressSignal`（本阶段给它补了 `to_dict`/`progress_signal_from_dict`）。
- `ProofWorkspace` 新增 `branches: tuple[ProofBranch, ...]`，经 `successor(branches=...)`、`to_dict`、`workspace_from_dict` 序列化；`initialize_from_task` 默认 `branches=()`。branches 随 workspace 序列化自动进 trace（Phase 3 已透传 `metadata["workspace"]`），不改 trace_store。
- 本阶段只交付数据结构 + 序列化 + workspace 接入 + 确定性 observation 提取器 + 测试。`build_controller` 对 `STRUCTURED` **仍抛** `StructuredModeUnavailableError`；不引入 `SearchAction`/`FailureHypothesis`（Phase 5）、不写 frontier/AND-OR 选择（Phase 6）、minimal 路径不 import workspace 包。

## Phase 5 统一 ProofAgent 动作与失败假设

- 动作协议原语补齐在 `agent/proof_system/workspace/` 子包：`SearchAction`/`SearchActionKind`/`MutationKind` + `DEFAULT_ALLOWED_MUTATIONS` 默认作用域表（`action.py`）、`FailureHypothesis`/`FailureKind`（`hypothesis.py`），全部 frozen + `to_dict`/`from_dict`。`SearchAction` 引用其他原语只靠字符串 id（branch/step），自身 branch-agnostic。
- 每个 `SearchAction` 显式声明 `allowed_mutations`（8 个 `MutationKind`：formal_specification / obligation / obligation_dependency / argument_step / lean_artifact / alignment_link / branch_status / new_structure；observation 不是 mutation kind，因为它是 append-only 证据）。`SearchAction.validate()` 确定性校验 target_branch_id 非空、rationale 非空、allowed_mutations 是 `DEFAULT_ALLOWED_MUTATIONS[kind]` 子集（**允许 narrow、禁止 broaden**，跨界须另起动作）、target_step_ids 非空唯一，返回 `SearchActionReport`，不抛。
- `FailureHypothesis` 承载多个竞争性失败假设：`evidence_ids`（非空，引用 Phase 4 `Observation`）、`confidence`、`affected_step_ids`（可空）、`proposed_tests`（`SearchAction` 元组，可空）。`FailureKind` 仅含 6 个模型竞争语义类别（theorem_misuse / argument_gap / insufficient_assumptions / alignment_mismatch / implementation_defect / capability_missing），**不含**基础设施错误——保持"假设 = 模型竞争产物"边界纯粹，基础设施错误由确定性规则单独处理。
- `FailureHypothesis.validate()` 确定性校验 hypothesis_id 非空、kind 合法、confidence ∈ [0,1] 且有限、evidence_ids 非空唯一、affected_step_ids 唯一、proposed_tests 委托 `SearchAction.validate()` 并以 `proposed_tests[i]:` 前缀聚合 child errors，返回 `FailureHypothesisReport`，不抛。
- 本阶段只交付数据结构 + 序列化 + 校验 + 测试。`build_controller` 对 `STRUCTURED` **仍抛** `StructuredModeUnavailableError`（消息措辞已更新为 frontier/AND-OR driver 是 Phase 6）；**不**生成假设、**不**执行动作、**不**把 `SearchAction` 接进 `ProofBranch`（wiring 是 Phase 6）、minimal 路径不 import workspace 包。

## `ChatDriver` 抽象

`ChatDriver` 是各 agent 共享的 chat-completion 驱动，职责单一：

- 组装 OpenAI 兼容请求 payload。
- 运行 tool-call loop（最多 `max_tool_rounds` 轮）。
- 最终请求支持 `n=final_n` 以获取多个候选。
- 分发 tool 调用到注册的 `Tool`。

新增 agent 时，通常只需：

1. 构造 `ChatDriver(config, transport, tools, max_tool_rounds)`。
2. 写 system/user prompt。
3. 调用 `driver.complete(messages, final_n=...)`。
4. 解析返回内容。

## 如何扩展新 Agent

### 1. 复用 `ChatDriver`

```python
from agent.agents import ChatConfig, ChatDriver, UrllibChatTransport

driver = ChatDriver(
    config=ChatConfig(api_key="...", model="..."),
    transport=UrllibChatTransport(),
    tools=tools,
    max_tool_rounds=5,
)
response = driver.complete(messages, final_n=1)
```

### 2. 作为 Proof 搜索中的 generator

实现 `ActionGenerator` 协议：

```python
from agent.search.action import ActionGenerator, ActionGenerationRequest, ActionCandidate

class MyTacticGenerator(ActionGenerator):
    def generate(self, request: ActionGenerationRequest) -> Sequence[ActionCandidate]:
        ...
```

然后在 `agent/cli/generators.py` 的 `build_action_generator` 中按 role / 配置选择使用。

### 3. 多模型配置（未来）

`AgentRole` 已定义：

```python
class AgentRole(str, Enum):
    FORMALIZER = "formalizer"
    PROOF_GENERATOR = "proof_generator"
    CONTEXT_MANAGER = "context_manager"
```

后续可按 role 读取不同环境变量（如 `OPENAI_MODEL_FORMALIZER`、`OPENAI_MODEL_PROOF_GENERATOR`、`OPENAI_MODEL_CONTEXT_MANAGER`）实现“便宜模型 tactic + 贵模型 prove”，或由轻量模型专门做上下文摘要。

## 关键边界协议

- `FormalizationAgent`：形式化入口。
- `ActionGenerator`：证明候选生成入口。
- `ProofSystemAdapter` / `LeanAdapter`：Lean checker 适配。
- `SafetyReviewer` / `StatementSafetyReviewer`：checker 接受后的安全审查边界。
- `ProofMemory` / `MemoryProcessor`：紧凑重试上下文及确定性更新边界。
- `Retriever`：检索接口（`ProofController` 使用）。
- `ScaffoldChecker`：scaffold 校验接口。

## CLI 入口

```bash
python -m agent.cli.app <source> --use-model
```

常用选项：`--use-model`、`--candidate`、`--max-checks`、`--max-model-calls`、`--enable-retrieval`、`--context-summarizer`、`--context-model`、`--proof-model`、`--formalizer-model`、`--model`、`--execution-mode`。

模型选择按 per-role -> generic `--model` -> 环境变量 `OPENAI_MODEL` 的顺序回退。例如 `--proof-model gpt-4` 只覆盖 proof generator，未指定时 fallback 到 `--model`，再未指定时 fallback 到 `OPENAI_MODEL`。

## 测试

```bash
python -m pytest tests/ -q
```

重点测试：

- `tests/test_formalization.py`：formalizer 校验、重试、缓存、tool 调用。
- `tests/test_model_adapter.py`：proof generator、transport 重试。
- `tests/test_chat_driver.py`：新抽象层单测。
- `tests/test_controller.py`：ProofController 主循环。
- `tests/test_goal_state.py`：结构化 goal state、fingerprint、sorry 标记。
- `tests/test_memory.py`：ProofMemory 更新、来源追踪和 prompt 表示。
- `tests/test_safety.py`：statement-preservation 与 anti-cheating 检查。
- `tests/test_trace_store.py`：原始 attempt、结构化 goal state、最终 memory 和 workspace 持久化。
- `tests/test_factory.py`：执行模式选择，structured 硬失败。
- `tests/test_workspace.py`：ProofWorkspace / ObligationGraph 数据结构、DAG 校验、版本规则、初始化与变异、branches 往返。
- `tests/test_assembler.py`：final-assembly 整体复检与 blocked 语义。
- `tests/test_argument.py`：ArgumentStep / ArgumentGraph 序列化与 DAG 校验。
- `tests/test_artifact.py`：LeanArtifact 字段序列化。
- `tests/test_alignment.py`：AlignmentLink / AlignmentRelation 往返。
- `tests/test_observation.py`：Observation 序列化与 checker 提取器。
- `tests/test_branch.py`：ProofBranch 嵌套序列化与状态枚举。
- `tests/test_search_action.py`：SearchAction 序列化、默认作用域表完整性、narrow/broaden 校验。
- `tests/test_hypothesis.py`：FailureHypothesis 序列化、confidence/evidence 校验、嵌套 proposed_tests 聚合。

## 注意事项

- `ChatConfig` 是 OpenAI 兼容配置，可用于任意 OpenAI-compatible endpoint。
- 真实 Lean checker 调用可能耗时较长，CLI/测试支持超时与重试。
- 持久化 Lean server 完成信号不可靠时，会在 `--lean-server-fallback-seconds` 静默期后接受当前诊断，避免每次都回退到子进程。
- `CheckResult.accepted` 表示 checker 的原始判断；controller 只有在后续 safety verdict 也通过时才返回 `ControllerResult.accepted=True`。
- `.env` 只由脚本消费，不要直接读取其内容写入日志。
