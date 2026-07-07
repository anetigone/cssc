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
│   ├── structured/           # Phase 6 StructuredController（frontier/AND-OR 搜索）
│   │   ├── controller/       # StructuredController 包（__init__ 向后兼容 re-export）
│   │   │   ├── core.py       # 主循环 / StructuredController
│   │   │   ├── actions.py    # capability/decompose/argument/representation 执行器
│   │   │   └── runtime.py    # 生成 / 渲染 / safety review 辅助
│   │   ├── proposal/         # typed action proposal 包（__init__ 向后兼容 re-export）
│   │   │   ├── core.py       # StructuredActionProposal / generator / legacy adapter
│   │   │   └── types.py      # ActionPayload union / payload dataclasses
│   │   ├── reducer/          # deterministic workspace transition 包（__init__ 向后兼容 re-export）
│   │   │   ├── core.py       # apply / StructuredActionResult / accept / failure / capability audit
│   │   │   ├── decompose.py  # DECOMPOSE 结构转移
│   │   │   └── structural.py # argument / representation / failure hypothesis 转移
│   │   ├── frontier.py       # Frontier / FrontierNode 调度器（只读 workspace）
│   │   ├── run_state.py      # _StructuredRunState + build_structured_result
│   │   └── solution_tracker.py # has_complete_solution / select_solution
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

> `controller`、`workspace`、`tools`、`structured` 已拆分为子模块包；每个包的 `__init__.py` 通过向后兼容的 re-export 保留原有公共 API，现有 `from agent.search.controller import ...`、`from agent.proof_system.workspace import ...`、`from agent.agents import ...`、`from agent.search.structured.controller import ...`、`from agent.search.structured.proposal import ...`、`from agent.search.structured.reducer import ...` 等导入路径无需修改。

## 导入风格

- 同一子模块包内优先使用相对导入（1~2 个点），保持重构时路径稳定。
- 当相对导入需要 3 个及以上点时，必须使用绝对导入，避免出现 `from ....x import ...` 这种过深路径。

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
- Phase 2 时仅实现 `minimal` 执行器（`ProofController`），`structured` 抛 `StructuredModeUnavailableError`；Phase 6 已落地 `StructuredController`，`build_controller` 现按模式返回对应执行器。`StructuredModeUnavailableError` 类保留作向后兼容 import + CLI 防御性 except + 未知 mode 兜底。两种模式仍**不**静默退化为对方，避免把 structured 运行误记成 minimal。
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
- 每个 `SearchAction` 显式声明 `allowed_mutations`（8 个 `MutationKind`：formal_specification / obligation / obligation_dependency / argument_step / lean_artifact / alignment_link / branch_status / new_structure；observation 不是 mutation kind，因为它是 append-only 证据）。只读 `DEFAULT_ALLOWED_MUTATIONS` 定义各动作的最大作用域；argument/implement 动作允许同步维护 alignment。`SearchAction.validate()` 确定性校验 target_branch_id、rationale、作用域和 target_step_ids，返回 `SearchActionReport`，不抛。
- `FailureHypothesis` 承载多个竞争性失败假设：`evidence_ids`（非空，引用 Phase 4 `Observation`）、`confidence`、`affected_step_ids`（可空）、`proposed_tests`（`SearchAction` 元组，可空）。`FailureKind` 仅含 6 个模型竞争语义类别（theorem_misuse / argument_gap / insufficient_assumptions / alignment_mismatch / implementation_defect / capability_missing），**不含**基础设施错误——保持"假设 = 模型竞争产物"边界纯粹，基础设施错误由确定性规则单独处理。
- `FailureHypothesis.validate()` 确定性校验 hypothesis_id 非空、kind 合法、confidence ∈ [0,1] 且有限、evidence_ids 非空唯一、affected_step_ids 唯一、proposed_tests 委托 `SearchAction.validate()` 并以 `proposed_tests[i]:` 前缀聚合 child errors，返回 `FailureHypothesisReport`，不抛。
- `ProofBranch` 将 `last_action` 与 `failure_hypotheses` 作为权威状态持久化，并校验 action target、observation evidence 和 affected step 引用；旧 `last_action_summary` 仅用于 Phase 4 trace 兼容。

## Phase 6 Frontier 与 AND-OR 搜索

- 结构化执行器在 `agent/search/structured/` 子包（与 `controller/` 平行、**互不 import**，minimal 不承担 structured 成本）：`frontier.py`（`Frontier` 可变调度器 + `FrontierNode` frozen 节点；只读 workspace、从不 mutate；pop 用稳定 tuple 排序 `(stalled_streak, depth_from_root, attempt_count, branch_id)`，确定性、trace 可重放；`_stalled_streak` 纯函数从 `branch.observations` 派生 trailing 相同 goal 指纹的 attempt 总数，reducer 复用同一值）、`solution_tracker.py`（`has_complete_solution`/`select_solution` 纯函数；判定每个 active obligation 是否有 version 相容的 ACCEPTED+artifact branch，与 `ArtifactAssembler.assemble` 前置条件对齐，避免 tracker 与 assembler 对就绪状态不一致）、`reducer/core.py`（`apply(workspace, action, result)` 纯函数，绝不 mutate，全部走 `replace` + `workspace.successor`）、`run_state.py`（`_StructuredRunState` + `build_structured_result`，平行 `results.py`、复用 `summarize_run`/`new_sample_id`，不改 minimal 的 `results.py`）、`controller/core.py`（`StructuredController`）。
- `StructuredController` 与 `ProofController` **同构造签名**（factory 切换零成本），`run(task)` 跑 plan1.md §12 的 structured 循环：`frontier.pop → 确定性选 IMPLEMENT/REPAIR_IMPLEMENTATION`（用 `DEFAULT_ALLOWED_MUTATIONS` 包装，复用现有 `ActionGenerator` 产出 proof body，**不**新增 SearchAction 模型生成器、**不**改 prompt 协议）`→ render+check（复用 AttemptWorkspace.write_candidate + adapter.check）→ safety（仅 check accepted 时）→ reducer.apply 折叠进不可变 workspace → frontier.update → has_complete_solution 命中则 _assemble_and_finalize`。
- 分支状态机全在 reducer：accepted+safety→`BranchStatus.ACCEPTED` + `register_accepted_fact`（obligation 同步 ACCEPTED）；check rejected→保持 ACTIVE、追加 `observations_from_check_result`、保留 `lean_artifact` 作 provenance（失败实现不否定数学策略）、更新 `last_action`；`stalled_streak >= STALL_THRESHOLD(3)`→DORMANT（终态，保证循环终止）；根策略 branch（无 parent）连续同 goal_fp 失败 `>= REPAIR_THRESHOLD(2)` 且尚未派生过子分支→派生 REPAIR 子 branch（新 `branch_id`、继承 argument/alignment/observations、`lean_artifact=None`，新尝试不覆盖旧分支）。
- 复用共享预算/metrics/trace 出口：每个 IMPLEMENT attempt=1 model_call+1 check；最终 assemble 额外 reserve 1 check（assembler 接收 `budget_slice` 但不自 reserve，controller 显式 `reserve_check`）；`metadata["workspace"]=workspace.to_dict()` 经 `trace_store.workspace_payload` 透传；`_StructuredRunState` 只持有 attempts/attempt_metrics/safety_rejections（权威状态在 workspace），不复用 minimal 的 `_ControllerRunState`。
- `factory.build_controller` 解锁 `STRUCTURED` 返回 `StructuredController`（lazy import，minimal import 图不拉 structured 包）；mode-mismatch 检查提公对两分支都跑；`StructuredModeUnavailableError` 类保留（向后兼容 import + CLI 防御性 except + 未知 mode 兜底）。
- 第一版只驱动单 root obligation（OR 搜索 + 分支状态机 + 终检 assembly）；retriever/context_summarizer 已接入但仍是 minimal 风格摘要（不是 plan1.md §10 的 workspace 投影），不 decompose、不自动恢复 DORMANT——structured 上下文投影、DECOMPOSE、能力审计是 Phase 7。

## Phase 7.0 端到端契约冻结

- 新增纯函数结果契约聚合层 `agent/search/structured/summary.py`：`build_result_summary(workspace, *, assembly_result=None, selected_branch_ids=())` 从 workspace + 可选 `AssemblyResult` 派生 frozen `ResultSummary`（+ 各 `*_from_dict`），含 accepted/open/blocked obligations、selected branches + preserved alternatives、assembly outcome（executed/accepted/errors）、workspace validation report，以及 `blocked_branch_obligation_ids`——显式冻结当前「branch BLOCKED 但 obligation 仍 OPEN」的不一致现状（Phase 7.3 能力审计才同步把 obligation 置 BLOCKED）。
- 零新依赖、不进 structured 包 `__all__`；minimal 路径不 import，不承担成本。
- `build_structured_result`（`run_state.py`）加可选参数 `assembly_outcome`：非 None 时透传 `metadata["assembly"]`（含 errors，修复原 assembly 失败时 `AssemblyResult.errors` 被丢弃、只留 `stop_reason="assembly_failed"` 的问题）并填充 `metadata["result_summary"]`；minimal 的 `build_final_result` 不动，两个 key 均为 structured 独有。
- `controller/core.py` 两处 `_assemble_and_finalize` 调用传 `assembly_outcome=assembly`（成功 / 失败两路径）；未进入 assembly 的终态（budget 耗尽、no_actions、tool_unavailable）保持 `assembly_outcome=None`，`ResultSummary.assembly.executed=False`。
- `tests/test_structured_e2e.py` 四测：单根接受契约、两 helper+root 纯数据结构层（不跑 controller 多义务循环，那是 Phase 7.4）、capability 缺失→blocked 现状冻结、assembly 失败 errors 透传回归。

## Phase 7.1 workspace context projection

- 新增纯函数投影模块 `agent/search/structured/projection.py`：`build_context_projection(workspace, branch_id)` 派生 frozen `StructuredContextProjection`（+ 各 `*_from_dict`，含 `context_projection_from_dict` 往返），字段覆盖 root、当前 obligation+版本、dependency closure（带 stale-fact version 守卫的 `DependencyFact`）、全部 accepted facts、argument steps（带 alignment_relation + aligned_declaration）、去重后 observation（`(goal_fingerprint, message)` 去重 + 尾部 `MAX_PROJECTED_OBSERVATIONS=12` cap）、failure hypotheses、同义务 sibling branches（`MAX_SIBLING_BRANCHES=8` cap）。零新依赖、不进 structured 包 `__all__`；minimal 路径不 import。
- projection 跨 structured→prompt 边界为 plain dict：`StructuredController._generation_metadata` 调 `build_context_projection`，把 `metadata["structured_projection"]=projection.to_dict()` 塞进 request，并从同一 projection 派生既有 `branch_obligation`/`verified_facts`/`previous_attempt`（形状不变，旧测试/既有渲染不破）——projection 成为单一数据源。`previous_attempt.observations` 用**去重后**的 projection observations，保证 ContextSummarizer 看到的证据 = prompt 渲染的证据，summarizer 只能压缩不能产生平行事实集。
- `ChatActionGenerator`（`agent/agents/proof.py:_append_structured_projection`）以 `Mapping`/`Sequence` 鸭子类型渲染 projection：当前 obligation 版本、dependency facts（verified 结论 + open 依赖 id）、argument steps 带 `[alignment_relation]`（含 `→ declaration`）、（仅 previous_attempt 缺失时的）proof body、去重 observations、failure hypotheses、sibling 状态。proof.py **不** import structured 包；minimal 永不设该键 → 渲染块整体跳过，零成本。
- 验收：prompt 确实出现当前 obligation、依赖事实、goal↔argument-step 对齐关系（`test_model_adapter.test_structured_projection_renders_in_prompt` 扩展断言）。

## Phase 7.2 typed structured action proposal

- 新增 `agent/search/structured/proposal/core.py`（payload  dataclasses 在 `proposal/types.py`）：typed `StructuredActionProposal`（携带自描述 `SearchAction` + `ActionPayload` union）+ `StructuredActionGenerator` Protocol + `SUPPORTED_PROPOSAL_KINDS`。payload 全部 frozen + `to_dict`/`*_from_dict`：`ImplementPayload`（覆盖 IMPLEMENT + REPAIR，kind 区别在 `SearchAction` 上）、`DecomposePayload`（+ `DecomposeChildSpec`）、`CapabilityTestPayload`。
- `validate()` 强制 kind/payload 一致并委托 `SearchAction.validate`；第一轮只开放 IMPLEMENT / REPAIR_IMPLEMENTATION / DECOMPOSE / RUN_CAPABILITY_TEST 四种 kind（其余 8 种后续 phase 开放）。零新依赖、不进 structured 包 `__all__`；minimal 路径不 import。
- `adapt_legacy_generator` 把旧 `ActionGenerator`（返回 `ActionCandidate`）逐个包成 IMPLEMENT 提案（`ImplementPayload`），idempotent；adapter 不持有 branch 状态，kind 设为 IMPLEMENT 占位并打 `LEGACY_KIND_DEFERRED` 标记，由 controller 在候选分支物化后按 `branch.last_action` 终定（`_finalize_kind`），完全复刻旧 `_pick_action` 语义。baseline 可比性不变。
- `StructuredController`：构造时 `adapt_legacy_generator` 归一化（检测 `_is_structured_generator` 标记避免重复包装，native generator 旁路）；删除 `_pick_action`，新增 `_finalize_kind`（deferred 提案终定 IMPLEMENT/REPAIR + rationale/作用域，native 提案直通）与 `_proposal_edit`（从 `ImplementPayload` 重建 `CandidateEdit`，`action` 取 `legacy_action`，默认 `model_complete`）。run 循环对非 implement kind 提案记录到 `state.skipped_proposals` 后 `continue`——这是 7.2 边界：DECOMPOSE / RUN_CAPABILITY_TEST 类型就位、可序列化、可校验，但不执行（执行器分别归 Phase 7.3 / 7.4）。legacy adapter 只产 implement，baseline 路径行为零变化。
- `_StructuredRunState` 加 `skipped_proposals` 字段，经 `build_structured_result` 透传到 `metadata["skipped_proposals"]`，让 trace 看见 native generator 发出但未执行的提案。
- 不变项：`action.py`（`ActionCandidate`/`ActionGenerator`/`StaticActionGenerator`）、`cli/generators.py`、`factory.py`、`workspace/action.py`、`reducer/core.py`/`frontier.py`/`branch_ops.py`（只读）/`finalize.py`/`projection.py`；minimal `ProofController` 不 import `proposal/core.py`，零成本。
- 测试：新增 `tests/test_structured_proposal.py`（payload/proposal 往返、validate kind/payload 一致 + 不支持 kind + broaden scope 拒绝、adapter 正确性 + idempotent + native 旁路）；`tests/test_structured_controller.py` 三条兼容性用例不变，新增 legacy→implement/repair kind 终定 + native `StructuredActionGenerator` 旁路（`skipped_proposals` 为空）两测。

## Phase 7.3 Capability audit 闭环

- 7.2 把 `RUN_CAPABILITY_TEST` 定为合法、可序列化、可校验的 proposal kind 但只记 `skipped_proposals` 跳过；7.3 真正执行它：`StructuredController._run_capability_audits` 在 IMPLEMENT 候选展开前逐个跑 capability proposal——把 `CapabilityTestPayload.signature` 作为 proof body 包进 `CandidateEdit`（`action="capability_test"`），复用 `_check`（`adapter.render_candidate` 替换 hole + `adapter.check`），每次 audit = 1 check、不另计 model_call（signature 由 generator 给定、不重新调模型）。结果经 reducer `apply` 折叠；DECOMPOSE 仍 skip（归 7.4）。audit 不跑 safety：`StructuredActionResult.safety_verdict=SafetyVerdict(accepted=False)` 占位，capability signature 接受 ≠ 命题成立。
- reducer 新增第三转移分支 `_apply_capability_audit`（`reducer/core.py`）：从 `check_result.category` 派生一条 `Observation`（`source=ObservationSource.CAPABILITY_AUDIT`、`raw_evidence_ref="capability:<N>"`、message 区分 available / probe failed）追加到 branch.observations。缺失判据 `_capability_missing(check_result)` 只认 `UNKNOWN_IDENTIFIER` / `INVALID_REFERENCE` / `TOOL_UNAVAILABLE` 三个 category（环境资源不可用的事实），其他失败（unsolved goals / type mismatch）保守不阻塞——capability audit 只能阻塞路线，不能判命题错。缺失时 branch 置 `BLOCKED` **且** obligation 经 `_block_obligation`（`graph.with_obligation` + `workspace.successor`，复刻 `register_accepted_fact` 手法）同步置 `ObligationStatus.BLOCKED`；可用时 branch 保持 ACTIVE、不注册 verified fact。
- 7.3 闭合 7.0 冻结的不一致：`ResultSummary.blocked_branch_obligation_ids`（`summary.py`）现在排除 obligation 已 BLOCKED 的条目，capability 缺失路径归零、`blocked_obligations` 填充；`no_actions` 路径（`block_branch` 只翻 branch）保持原样——缺候选 ≠ 机械能力缺失，那是另一种 gap，不在 7.3 范围。
- frontier 不动：BLOCKED branch 经 `status != ACTIVE` 自动掉队，无 ACTIVE branch 时 `frontier.has_work()` 为 False 自然终止循环。
- 不变项：`branch_ops.block_branch`（no_actions 仍只翻 branch）、`workspace/action.py`（`RUN_CAPABILITY_TEST` 作用域早已为空集）、`proposal/core.py`（kind/payload 协议不变）；minimal `ProofController` 不 import structured 包，零成本。
- 测试：`tests/test_reducer.py` 新增 `ReducerCapabilityAuditTests`（缺失→branch+obligation BLOCKED、可用→ACTIVE 不注册 fact、非缺失失败不阻塞、不可变性 4 测）；`tests/test_structured_controller.py` 新增 native capability generator 缺失阻塞 + 可用保持 ACTIVE 两测；`tests/test_structured_e2e.py` 旧 `test_capability_missing_blocked_semantics` 改名 `test_no_actions_blocks_branch_leaving_obligation_open`（冻结 no_actions 残留 gap），新增 `test_capability_missing_blocks_obligation`（capability 闭环：branch+obligation 双 BLOCKED、gap 归零）。

## Phase 7.4 DECOMPOSE 执行器与依赖感知 frontier（多义务闭环）

- 7.2 把 `DECOMPOSE` 定为合法、可序列化、可校验的 proposal kind 但只记 `skipped_proposals` 跳过；7.4 真正执行它，并打通多义务（AND-OR）搜索：root → decompose 出 helper → helper 各自 IMPLEMENT accepted → parent（依赖满足后）IMPLEMENT accepted → 整体 assembly 通过。`StructuredController._run_decompose`（平行 `_run_capability_audits`）把每个 `DecomposePayload.children` 折叠进 workspace：不消费 check/model_call（结构性转移，children statement 来自 generator payload）。
- reducer 新增**独立入口** `apply_decompose(workspace, action, *, children, parent_branch_id)`（不污染 `StructuredActionResult`——decompose 无 check/safety/artifact）：校验目标 branch 的 obligation 是当前版本（防对已 superseded 再 decompose，否则 no-op），把每个 `DecomposeChildSpec` 造 `ProofObligation`（`dependency_ids` 收窄到兄弟集），调 `workspace.decompose` 插 children + parent `new_version`，**在同一 successor** 把所有 pin 旧 parent 版本的 branch 置 `SUPERSEDED`（否则 `workspace.validate` 报"branch remains ACTIVE on superseded obligation"——load-bearing invariant），加一个 ACTIVE branch pin 新 parent 版本（新 `branch_id` `<parent>.p<n>`）+ 每个 child 一个 ACTIVE branch（`<parent>.d.<child_id>`）。version drift 由 `ObligationGraph.by_id` 天然处理（parent 存 child id，re-decompose 后解析到新版本）。
- frontier 加 **readiness gate**（`_is_ready`，最关键的正确性点）：一个 branch 可调度当且仅当 ACTIVE + obligation 可解（OPEN/IN_PROGRESS）+ 所有 `dependency_id` 经 `by_id` 解析后 ACCEPTED。**readiness 是 gate 不是 sort**——不 ready 的 branch 排除出 `_pending`（`seed`/`update`），而非降权；否则 not-ready parent 被反复 pop 烧预算、且循环不终止。helper accepted 后 parent 在下次 `update`（从头重建 pending）自动入队；helper BLOCKED 后 parent 永不入队、`has_work()` False 自然终止。单 root baseline：root 无 dependency，`_is_ready` 恒 True，行为不变。`_priority_key` 不动（ready 集内仍按 stall/depth/attempt/branch_id）。
- artifact **kind/rendering contract**：`artifact.py` 加 `ArtifactKind { PROOF_BODY, DECLARATION }`（默认 PROOF_BODY 保单 root 不变）。`ArtifactAssembler` 重构——root artifact（PROOF_BODY）填 task 的 proof hole 恰好一次，helper artifact（DECLARATION）作为独立顶层声明经 `_inject_helpers` 注入 import/open 之后、root 声明之前（单 root 路径逐字节不变，`test_final_assembly_inserts_proof_snippet_only_once` 仍绿）；assembler 断言单 root。
- `VerifiedFact` 加 `declaration_id` / `artifact_source`（默认 None，向后兼容）；`register_accepted_fact` 增默认 kw。reducer `_accept` 的 fact.statement 改为镜像 artifact 的 rendered source（root → proof body，baseline 不变；helper → 完整声明，parent prompt 可按名复用），并透传 declaration_id/artifact_source。
- controller `_render_target`：helper obligation 的 IMPLEMENT 候选按自己的 `lean_statement`（带 hole 模板）独立渲染 + check（而非塞进 root hole），保证真实 Lean 下 helper 被独立验证；root 仍填 task hole。`_StructuredRunState.decompose_records` 经 `build_structured_result` 透传到 `metadata["decompose_records"]`。
- safety `StatementSafetyReviewer._statement_preservation_reason` 从 `startswith` 改为**子串存在**：多义务 assembly 在 root 之前注入 helper 声明，源文件不再以 root statement 开头；shortcut/axiom 扫描仍跑全文防 cheating。minimal 路径 prefix 在 offset 0，行为不变。
- frontier drain 且无终态原因时 `stop_reason="no_ready_work"`；若存在 active BLOCKED obligation 升级为 `"blocked"`。
- 不变项：`proposal/core.py`/`proposal/types.py`（kind/payload 协议不变）、`branch_ops`、`solution_tracker`/`summary`/`finalize`；minimal `ProofController` 不 import structured 包，零成本。
- 测试：`tests/test_reducer.py` 新增 `ReducerDecomposeTests`（supersede+seed+validate、空 children no-op、stale 版本 no-op）+ `ReducerArtifactContractTests`（helper fact=渲染声明/root fact=proof body、artifact kind）；`tests/test_frontier.py` 新增 `FrontierReadinessGateTests`（parent 未 ready / helper accepted 后 ready / helper blocked 终止 / 单 root baseline）；`tests/test_assembler.py` 新增多义务注入 + 单 root 不变两测；`tests/test_safety.py` 新增前置 helper 声明不触发 statement_not_preserved；`tests/test_structured_controller.py` 旧 `test_non_executable_native_proposal_*` 改写为 `test_native_decompose_executes_and_structures_the_workspace`（7.4 执行语义），新增多义务端到端（decompose→helper accept→parent accept→assembly ok）+ helper blocked→`stop_reason="blocked"` 两测。

## Phase 7.5 helper 复用语义清理

- projection `AcceptedFactSlot` / `DependencyFact` 加 `declaration_id`（`projection/slots.py`）：`_accepted_facts` / `_dependency_facts`（`projection/core.py`）从 `VerifiedFact.declaration_id` 填充，`to_dict`/`from_dict` 往返。`proof_projection.append_structured_projection` 渲染已验证依赖时前缀声明名（`helper1: <statement>`），让 parent prompt 按名引用 helper 而非重新推导声明。
- controller `stop_reason` 细化：`no_ready_work` 终态下若存在 active BLOCKED obligation 升级为 `"blocked"`（机械死终 vs ready work 耗尽的区分；与 plan7.7 PARTIAL 方向一致，但 7.5 只做 stop reason，不引入 `WorkspaceStatus.PARTIAL`）。
- 不变项：artifact/reducer/assembler 契约在 7.4 已就位（7.5 不再改）；`WorkspaceStatus.PARTIAL`、transitive BLOCKED propagation、自动 DORMANT 恢复在 7.7 落地（见 Phase 7.7 段）。
- 测试：`tests/test_structured_projection.py` 新增 `declaration_id` 透传 + 往返；`tests/test_structured_controller.py` 新增 helper blocked→`stop_reason="blocked"`。

## Phase 7.6 argument/representation 执行 + 竞争性失败假设

- 三种 argument/representation kind 真正落地执行（之前只存在于 `action.py` 默认作用域表，proposal `validate()` 直接拒）。`proposal/core.py` 加 `ArgumentStepSpec`/`AlignmentSpec`（payload 层纯数据描述，不耦合 argument/alignment 模块）+ `ProposeArgumentPayload`/`RefineArgumentPayload`/`ChangeRepresentationPayload`（frozen + `to_dict`/`*_from_dict`），三个 discriminator 常量，扩 `ActionPayload` union 与 `SUPPORTED_PROPOSAL_KINDS`，`validate()` 加 kind→payload 一致校验，`structured_action_proposal_from_dict` 加分发分支。
- reducer 三个纯结构入口（与 `apply_decompose` 平行，无 `CheckResult`/safety/artifact）：`apply_argument`（PROPOSE 追加 step+alignment、REFINE 按 step_id 替换 claim+alignment；每个 step 必须有 alignment 的硬规则在**同一次转移**配对，pre-commit 校验候选 branch，失败 no-op，REFINE 全未命中现有 step 也 no-op）、`apply_change_representation`（父 branch SUPERSEDED + 派生 `<parent>.rep<n>` child，继承 observations、`lean_artifact=None`，整体替换 argument/alignment，pre-commit 校验 child）、`apply_failure_hypotheses`（逐个校验 evidence⊆observation_ids / affected⊆step_ids / proposed_tests.target==branch_id / hypothesis_id 不重复，**丢弃**非法 hypothesis，无合法则不产生 successor）。共享构造器 `_to_argument_step`/`_to_alignment_link`（UNALIGNED 强制三 target=None）/`_alignments_cover`/`_next_representation_branch_id`。
- controller 主循环分流加 `argument_proposals`（PROPOSE/REFINE）+ `representation_proposals`（CHANGE_REPRESENTATION）两个桶，在 capability/decompose 之后、IMPLEMENT 之前处理，处理后 `continue` 刷新 frontier（仿 `_run_decompose`）；新增 `_run_argument`/`_run_change_representation` batch 执行器。**竞争性假设由 native generator 在后续 `generate()` 携带**（proposal metadata 的 `FAILURE_HYPOTHESES_KEY`，不新增独立模型调用，保持「每 IMPLEMENT attempt = 1 model_call + 1 check」预算），controller `_fold_failure_hypotheses` 在 `_generate` 后立即折叠到当前 branch（此时失败 observation 已在 branch 上）；`_select_test_action` 按 kind 成本（capability/decompose < argument/representation < implement）+ confidence 降序选一个 `proposed_tests`。
- `run_state.py` `_StructuredRunState` 加 `argument_records`/`representation_records`，`build_structured_result` 透传 `metadata["argument_records"]`/`metadata["representation_records"]`；hypothesis 不单独记字段，随 `metadata["workspace"]`（branch.failure_hypotheses）进 trace。
- 不变项：`branch.py`/`hypothesis.py`/`argument.py`/`alignment.py`/`action.py` 只读引用（7.6 不改）；projection `core.py`/`slots.py` 已投影 `argument_steps`+`failure_hypotheses`，7.6 只填真内容，`proposed_tests` 不进 projection（是 controller 待执行动作，非静态上下文）；minimal `ProofController` 不 import structured 包，零成本。
- 测试：`tests/test_structured_proposal.py` 加 3 payload 往返 + proposal 往返 + kind/payload 一致 + 跨 kind 拒绝；`tests/test_reducer.py` 加 `ReducerArgumentTests`（PROPOSE 同转移 step+alignment 校验绿、纯结构无 artifact、缺 alignment no-op、REFINE 替换/未知 id no-op、不可变）/`ReducerChangeRepresentationTests`（父 SUPERSEDED+`.rep0` child ACTIVE、继承 observations、id 确定性、缺 alignment no-op）/`ReducerFailureHypothesisTests`（append 校验、非法 evidence/错误 branch test/重复 id 丢弃、不可变）；`tests/test_structured_controller.py` 加 native PROPOSE_ARGUMENT 执行、CHANGE_REPRESENTATION fork、失败后竞争 hypothesis 折叠进 projection、`_select_test_action` 低成本优先。

## Phase 7.7 Partial result、transitive BLOCKED 与 evidence-driven DORMANT 恢复

- `WorkspaceStatus` 加 `PARTIAL`（`workspace/spec.py`）；序列化往返自动支持，`ProofWorkspace.validate()` 本不约束 status，无校验改动。
- **确定性终态 finalizer** `run_state.py:finalize_workspace_status(workspace, *, accepted)`：`accepted=True`→`ACCEPTED`；否则若所有 active obligation 都不在 `{OPEN, IN_PROGRESS}`（无活路线）且 root 非 accepted→`BLOCKED`；否则若存在任一**非根** active obligation `ACCEPTED`→`PARTIAL`（保留可复用部分结果）；否则保持 `SEARCHING`（单根/无 helper 的 run 预算中途耗尽，不误标 PARTIAL——判据是「非根 accepted」，单根 baseline 命中不到）。`build_structured_result` 在派生 `ResultSummary` 前对 workspace 跑一次 `successor(status=...)`，终态同时进 `metadata["workspace"]` 与 `result_summary.workspace_status`；accepted 路径幂等（`finalize.py` 已设 ACCEPTED）。`summary.py` 不改（直接读 `workspace.status.value`）。
- **传递性 BLOCKED 传播** `reducer/core.py:_block_obligation`：capability 缺失把 helper obligation 置 BLOCKED 时，沿**反向依赖边**（谁是它的依赖者）BFS，把所有依赖闭包包含被阻塞 obligation 的 active、仍 `{OPEN, IN_PROGRESS}` 的 obligation 同步置 BLOCKED，全部在**同一个 successor**（一次 `obligation_graph` 批量替换）。已终态（ACCEPTED/BLOCKED/SUPERSEDED）的不动——验证过的 fact 保持有效，superseded 是 provenance。`block_branch`（no_actions 路径）**不**走这里：它只翻 branch、留 obligation OPEN（缺候选 ≠ 机械能力缺失，是另一种 gap）。
- **Evidence-driven DORMANT 恢复** `reducer/core.py:_reactivate_dormant(workspace, *, trigger_obligation_id)`：寻找 obligation 等于 trigger、或其依赖闭包包含 trigger 的 **DORMANT** 分支，翻回 `ACTIVE`（一次 successor），不动 observations/argument。两个调用点，都在 reducer 内证据落地后立即触发：`_accept`（`register_accepted_fact` 之后——新依赖 fact accepted 是最强复活信号）与 `_apply_capability_audit`（capability **可用**分支——audit 找到可用资源）。**不是** frontier 驱动：frontier 已按 `_is_ready`（`status==ACTIVE`）从 workspace 重建 pending，复活分支自动重入队——「不因 frontier 空就无条件唤醒」，只在证据转移点复活；下一次 attempt 若 goal 指纹不变会再次 stall（自我纠正，不制造重复循环）。`CHANGE_REPRESENTATION` 路径不触发（父置 SUPERSEDED 非 DORMANT，无 DORMANT 可救）。
- 不变项：`branch.py`/`obligation.py`/`graph.py`/`action.py`/`hypothesis.py`/`proposal/*`/`projection*`/`frontier.py`/`finalize.py`/`solution_tracker.py`/`summary.py`/`branch_ops.py` 只读引用；minimal `ProofController` 不 import structured 包，零成本。
- 测试：`tests/test_reducer.py` 加 `ReducerTransitiveBlockTests`（helper block→依赖链 parent 同步 BLOCKED、已验证 sibling 不受影响、`block_branch` 路径不传播、不可变）+ `ReducerDormantRecoveryTests`（accept helper 复活 DORMANT parent、capability 可用复活、missing 不复活、无 DORMANT no-op）；新增 `tests/test_structured_run_state.py`（finalizer 五态：accepted / 单根 OPEN 耗尽→SEARCHING / 非根 accepted→PARTIAL / 全 blocked→BLOCKED / open+verified helper→PARTIAL）；`tests/test_workspace.py` 加 `PARTIAL` 序列化往返；`tests/test_structured_controller.py` 的 blocked-helper 测试加断言 root obligation 传递性 BLOCKED + `workspace_status="blocked"`。

## Phase 7.8 真实复杂任务与消融（**未做**）

- 这是 proof-system-redesign 的收尾实证对比：用同一组复杂 Lean 任务跑各档配置（minimal / structured 去掉某些能力），度量结构化层相对 minimal baseline 的净收益（plan1.md §15 的消融原则）。
- **依赖真实 Lean toolchain（慢、需非沙箱）+ 真实 model 调用（有 token 成本）**，不进 CI、手动跑。
- 完整方案（任务集、消融档位、跑批脚本、对比指标口径、报告产出、提交粒度）见 [tmp/phase7_8_plan.md](tmp/phase7_8_plan.md)。在落地前，本文不在此维护 7.8 的代码结构细节——它不引入新代码模块，只复用现有 structured 执行器 + trace 指标出口。

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
- `tests/test_frontier.py`：Frontier seed/pop 排序确定性、stalled_streak 派生、retired 分支不重新入队、REPAIR 子分支优先级；Phase 7.4 readiness gate（parent 未 ready / helper accepted 后 ready / helper blocked 终止 / 单 root baseline）。
- `tests/test_solution_tracker.py`：has_complete_solution / select_solution 的 version 相容、artifact 必需、stale 版本拒绝、多 accepted 取最小 branch_id。
- `tests/test_reducer.py`：apply 不可变转移、accepted/failure/safety 三态、DORMANT 与 REPAIR 子分支派生、原 workspace 不被 mutate；Phase 7.3 capability audit 三态；Phase 7.4 `apply_decompose`（supersede+seed+validate、no-op 守卫）+ artifact contract（helper/root fact statement 与 kind）；Phase 7.6 `apply_argument`（PROPOSE/REFINE 同转移 step+alignment、缺 alignment no-op、REFINE 未知 id no-op、不可变）/`apply_change_representation`（父 SUPERSEDED+`.rep0` child、继承 observations、id 确定性）/`apply_failure_hypotheses`（非法 evidence/错误 branch test/重复 id 丢弃）；Phase 7.7 `ReducerTransitiveBlockTests`（helper block→依赖链 parent 同步 BLOCKED、已验证 sibling 不受影响、`block_branch` 不传播、不可变）/`ReducerDormantRecoveryTests`（accept helper 复活 DORMANT parent、capability 可用复活、missing 不复活、无 DORMANT no-op）。
- `tests/test_assembler.py`：final-assembly 整体复检与 blocked 语义；Phase 7.4 多义务 helper 注入 + 单 root 不变。
- `tests/test_structured_controller.py`：StructuredController 主循环、metadata["workspace"] 透传、metrics.execution_mode、预算耗尽、REPAIR 派生、safety 拒绝、assemble 预算独立 reserve、tool_unavailable 短路、config mode 校验；Phase 7.2 kind 终定 / native 旁路；Phase 7.3 capability 缺失阻塞 / 可用保持；Phase 7.4 native decompose 执行 + 多义务端到端（decompose→helper accept→parent accept→assembly ok）；Phase 7.5 helper blocked→`stop_reason="blocked"`；Phase 7.6 native PROPOSE_ARGUMENT 执行、CHANGE_REPRESENTATION fork、失败后竞争 hypothesis 折叠进 projection、`_select_test_action` 低成本优先；Phase 7.7 blocked helper 传递性 BLOCKED root obligation + `workspace_status="blocked"`。
- `tests/test_structured_run_state.py`：Phase 7.7 终态 finalizer 五态（accepted→ACCEPTED / 单根 OPEN 预算耗尽→SEARCHING / 非根 accepted→PARTIAL / 全 blocked→BLOCKED / open+verified helper→PARTIAL）。
- `tests/test_structured_e2e.py`：Phase 7.0 端到端契约——单根接受契约、两 helper+root 纯数据结构层（decompose/序列化往返/assembler 前置）、capability 缺失→blocked 现状冻结、assembly 失败 errors 透传回归。
- `tests/test_structured_projection.py`：Phase 7.1 workspace context projection；Phase 7.5 helper fact `declaration_id` 透传 + 往返。
- `tests/test_structured_proposal.py`：Phase 7.2 typed structured action proposal——payload/proposal 往返、validate kind/payload 一致 + 不支持 kind + broaden scope 拒绝、legacy adapter 正确性/idempotent/native 旁路；Phase 7.6 argument/representation payload 往返 + kind/payload 一致 + 跨 kind 拒绝。
- `tests/test_safety.py`：statement-preservation 与 anti-cheating 检查；Phase 7.4 前置 helper 声明不触发 statement_not_preserved。

## 注意事项

- `ChatConfig` 是 OpenAI 兼容配置，可用于任意 OpenAI-compatible endpoint。
- 真实 Lean checker 调用可能耗时较长，CLI/测试支持超时与重试。
- 持久化 Lean server 完成信号不可靠时，会在 `--lean-server-fallback-seconds` 静默期后接受当前诊断，避免每次都回退到子进程。
- `CheckResult.accepted` 表示 checker 的原始判断；controller 只有在后续 safety verdict 也通过时才返回 `ControllerResult.accepted=True`。
- `ChatActionGenerator` 的截断或空模型输出属于 typed generation failure：`finish_reason="length"` 映射为 `generation:model_output_truncated`，不得折叠成 `no_actions` 或阻塞 structured branch；只有生成器正常返回空动作集合才使用 `no_actions`。
- token 成本采用跨 provider 可比口径：`model_input_tokens` 统计 prompt/input，`model_output_tokens` 统计可见 completion/output；provider 明确报告的 hidden reasoning tokens 从 output 中扣除并只保留在 `metadata["model_usage"]` 诊断记录中。未报告 reasoning details 的模型直接使用其 completion/output tokens。tool loop 内各轮请求累计一次。
- `.env` 只由脚本消费，不要直接读取其内容写入日志。
