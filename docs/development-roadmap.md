# Development Roadmap

本文集中维护项目的阶段迭代、历史决策、当前完成状态和后续计划。`agents.md` 与 `CLAUDE.md` 只描述当前工程事实并引用本文，不再复制阶段日志。

## 文档状态约定

- **完成**：实现和相关测试已落地。
- **部分完成**：主体存在，但实验或公共闭环尚缺。
- **历史/回归**：不再作为当前研究证据，只保留兼容与测试价值。
- 详细历史方案位于 `tmp/`；它们不是当前 API 的权威说明。

## 已完成的基础能力

### Baseline 与 minimal refinement

完成内容：原始 attempt/run metrics、结构化 Lean goal feedback、紧凑 `ProofMemory`、确定性 memory 更新、statement-preservation 与 anti-cheating safety review、trace provenance。

### 显式执行模式

完成内容：`minimal` / `structured` 参数化选择、factory 唯一构造点、共同观测字段、运行内禁止自动切换、minimal 与 structured import/cost 隔离。

### Structured workspace

完成内容：`ProofWorkspace`、obligation DAG、版本规则、verified facts、proof branches、argument graph、Lean artifacts、alignment、observations、typed search actions、competitive failure hypotheses 和不可变序列化。

### Structured search loop

完成内容：frontier、reducer、solution tracker、repair branch、capability audit、decompose、多 obligation readiness、helper reuse、argument/representation actions、partial/blocked 状态、evidence-driven dormant recovery 和 final assembly。

### Context 与 typed proposal

完成内容：workspace context projection、typed structured action proposal、legacy generator adapter、projection 到 prompt/summarizer 的单一数据源。

### 成本感知搜索

完成内容：run/branch/obligation 成本投影、cost-aware frontier、软预算、value-per-cost priority、action-level proposal cache、冻结历史成本估计、统一 budget snapshot、cheap/strong model routing 和 append-only cost ledger。

### 多洞任务链

完成内容：Formalizer 输出经 TaskBuilder 拆成单 active-hole 任务；后继任务使用 dependency marker；只有 checker+safety accepted proof 才能 materialize；CLI 可顺序运行依赖任务链并汇总子任务消耗。

## 历史阶段索引

| 里程碑 | 状态 | 历史资料 |
|---|---|---|
| baseline / minimal core / execution modes | 完成 | [`tmp/plan1.md`](../tmp/plan1.md) |
| workspace、branch、action、AND-OR controller | 完成 | [`tmp/phase7_plan.md`](../tmp/phase7_plan.md) |
| structured actions 与复杂任务闭环 | 完成 | [`tmp/phase7_8_plan.md`](../tmp/phase7_8_plan.md) |
| branch-level cost-aware policies | 完成 | [`tmp/phase8_plan.md`](../tmp/phase8_plan.md) |
| action-level cost runtime 与 routing | 部分完成 | [`tmp/phase9_cost_runtime_plan.md`](../tmp/phase9_cost_runtime_plan.md) |
| 内部行为型 benchmark | 历史/回归 | [`tmp/phase10_internal_benchmark_plan.md`](../tmp/phase10_internal_benchmark_plan.md) |

旧 `phase8_*` / `phase10_*` benchmark 入口和旧 `estimator_version` 字符串暂时保留，因为它们进入测试、trace、snapshot 或外部脚本。它们是兼容名称，不表示当前仍按这些阶段开发。

## 当前缺口

### 成本运行时与报告

- minimal、structured legacy 和 action runtime 必须使用同等级的 provider/tool/checker ledger。
- token 统计必须覆盖 cached、reasoning 和 billed/provider total；缺失数据不得默认为零。
- USD 使用 provider charge 或显式冻结价格表，并报告 measurement coverage。
- benchmark report 仍需支持 paired comparison、bootstrap CI、accepted-rate non-inferiority、成本节省和同成本成功率增益。
- 多任务链需要明确全局预算与每 obligation 预算的关系，并在 trace 中保留完整子任务成本。

### 外部 benchmark

- 为至少两个公开 Lean benchmark 建立独立 adapter、固定 split、license/source provenance 和 statement hash。
- 每个 benchmark 固定 Lean toolchain、Mathlib revision、Lake dependencies 与 checker 配置。
- 在模型运行前冻结 eligibility；失败后不得按 arm 结果删题。
- ground-truth proof 只用于构造验证，不能进入模型 prompt 或 retrieval。
- 内部 canary 只做 deterministic replay、trace schema、ledger 和 controller regression。

当前 miniF2F 进度：已提供针对外部 Google DeepMind Lean 4 checkout 的离线 preparation
adapter。它固定并记录 source revision、244/244 split、license、statement/scaffold hash、
Lean toolchain 与 Lake dependency revision，将聚合文件拆成独立单洞任务，并隔离上游 proof。
revision `f0a20e14c1eeccd859d51bb4c2b3ee487889c303` 的 488 个生成 scaffold 已完成
真实 Lean 4.27 eligibility：跨题标识符引用审计为零，split 聚合检查失败时自动二分到
单题，最终 488/488 eligible、0 ineligible、0 infrastructure failure。benchmark-specific
live runner 和正式实验冻结仍未完成。

### 公平 baseline 与消融

- 提供显式 `allowed_action_kinds` 或等价 action mask。
- 在相同 structured prompt、generator、workspace、frontier 和预算下比较 attempt/terminate、implement/repair 和 richer action space。
- 分离 action-space 收益、cost-aware selection 收益和 cheap/strong routing 收益。
- 真实任务上报告 richer-only action 的 proposal/execution/acceptance 和独占成功案例，不预设题目必须触发某种 action。

## 外部评测路线

### 1. 工程预检

- 冻结 benchmark source 和 project registry。
- masked scaffold 可 elaboration；ground-truth 回填可通过 checker+safety。
- 每个任务恰好一个 active hole；多洞任务依赖可 materialize。
- 所有 arm 使用同一 trace schema、cost ledger 和基础预算定义。

### 2. Live pilot

- 只检查 timeout、prompt 长度、usage coverage、自然 action exposure 和预算尺度。
- pilot 后冻结 task ids、模型版本、temperature/seed、prompt/protocol、价格表、history snapshot 和 controller 配置。
- 不依据各 arm 成功率筛题。

### 3. 正式实验

至少运行：

```text
2 public benchmarks × 2 models × preregistered arms × repeated runs
```

按 task/repetition 配对或轮换 arm 顺序，基础设施失败与数学失败分开处理，原始 trace 不覆盖。

### 4. 主结果门槛

与共享模型、prompt 信息、retrieval、checker 和预算的强 baseline 比较，至少满足一项：

1. checker+safety accepted rate 非劣，同时实际 token 或 USD 稳定节省 15–25%；
2. 相同实际成本下 accepted rate 绝对提高 3–5 个百分点。

此外必须通过 action mask 消融证明 richer action space 优于 attempt/terminate router，而不是由更多调用、更强模型或更宽预算造成。

建议预注册：accepted-rate 非劣界 1 个百分点；样本较小时最多放宽到 2 个百分点，并在实验前声明。结果应按每个 benchmark–model 单元格和总体分别报告，而不只给 pooled mean。

## 文档维护规则

- 新迭代只在本文增加里程碑或状态，不在 `agents.md`、`CLAUDE.md` 和代码 docstring 复制阶段编号。
- 完成后把“计划动作”改写为当前事实，并同步相关测试/入口说明。
- 历史计划允许保留原始阶段编号；若其决策已经作废，在本文标记 superseded/历史用途。
