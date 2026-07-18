# 外部 Benchmark 接入与运行规范

本文是项目内 benchmark 工程契约的权威说明，覆盖新公开 Lean benchmark 的接入、准备、
eligibility、live runner、失败分类、断点续跑、trace、成本与正式报告。阶段完成状态和最终研究
门槛仍由 [`development-roadmap.md`](development-roadmap.md) 维护。

`scripts/` 只保留可执行入口，不维护 Markdown 文档。脚本参数或流程变化时，应同步更新本文，
而不是在脚本目录创建局部说明。

## 适用范围与核心原则

外部 benchmark 指来自本仓库之外、具有固定题目与公开来源的 Lean 数据集。正式证据必须满足：

- 冻结 source URL、revision、split、license、statement hash 和项目依赖；
- benchmark 原始数据、生成数据与实验输出相互隔离；
- ground-truth proof 不进入 prompt、retry context 或 retrieval；
- 模型运行前完成 eligibility，不能根据某个 arm 的结果事后删题；
- 所有 arm 共享 checker、safety、trace、cost ledger 和基础预算语义；
- checker+safety accepted、证明失败与基础设施失败分开统计；
- 原始 observation、trace 和 ledger append-only，不用摘要覆盖原始证据；
- unavailable measurement 不得写成零，模拟成本不得作为真实 billed-cost 证据。

内部 canary 只用于 deterministic replay、trace schema、ledger 和 controller regression，不能替代
公开 benchmark 的主结果。

## 代码与目录边界

### 代码分层

共享逻辑放在 suite-neutral 层，benchmark-specific 逻辑通过 adapter 接入：

```text
agent/benchmarks/
├── <suite>.py                 # source extraction / preparation / validation
├── <suite>_eligibility.py     # checker eligibility gate
└── <suite>_runner.py          # same-process live runner

scripts/
├── <suite>_prepare.py         # thin CLI entry point
├── <suite>_eligibility.py
├── <suite>_benchmark_run.py
└── trace_pretty.py            # suite-neutral trace reader
```

`scripts/benchmark_harness.py` 是 suite-neutral 历史/共享入口。新增 suite 应配置或复用共享层，不得
通过修改历史模块全局变量接入。公共包的 `__init__.py` 继续维护稳定 re-export；内部拆分不得无意
破坏现有导入路径。

### 数据与输出

外部 checkout 和生成 fixture 不提交到仓库：

```text
benchmark/
├── <suite>/                   # upstream checkout，视为只读
└── generated/<suite>/         # scaffold、manifest、provenance、eligibility evidence

.runs/benchmarks/<suite>/<run-name>/
├── run.json                   # 冻结配置与 config hash
├── summary.json               # 可重建的进度摘要
└── tasks/<task-id>/
    ├── result.json
    ├── trace.jsonl
    └── candidates/
```

- `benchmark/` 存放 source/preparation 数据；`.runs/` 只存实际实验记录。
- runner 不修改 upstream checkout。
- manifest/provenance 必须能从冻结 source 重新生成和校验。
- summary 是派生视图；发生冲突时，以 per-task result、trace 和 provenance 为准。
- `summary.json.error_history` 在 resume 间持久保留去重后的既往错误；即使题目随后重跑成功，
  当前 `failed_tasks` 会清空，但对应的历史错误不会丢失。

## 新 Benchmark Adapter 契约

### 1. Source identity 与 provenance

准备阶段至少记录：

- suite 名称和 adapter/schema version；
- canonical source URL、不可变 revision/commit 和 dirty 状态；
- license 文件路径、内容 hash 和适用范围；
- Lean toolchain、Mathlib revision、Lake dependencies 与 project 文件 hash；
- 上游文件路径、split 来源和 task id 映射规则；
- statement、scaffold、原始 declaration 和生成 fixture 的 SHA-256。

正式准备默认拒绝 dirty checkout。若为修复或研究明确允许 dirty source，必须在 provenance 中记录，
且不能与正式冻结结果混用。

### 2. Task extraction 与规范化

- task id 在 suite 内唯一且稳定；不同 split 不得重叠。
- 排除 variant、辅助 proof 或非 canonical declaration 的规则必须显式、确定且可测试。
- 每个 `ProofTask` 只暴露一个 active `{{proof}}`。
- 同一 declaration 多洞必须先拆成不同声明；源码多洞依赖遵守 TaskBuilder dependency marker 规则。
- ground truth 只可用于离线回填验证，生成 scaffold 必须移除 proof body。
- fixture 必须经 `LeanTaskBuilder` round-trip 后仍得到同一 task id、statement 和唯一 active hole。
- adapter 不得通过读取模型结果来决定 task 是否进入数据集。

### 3. Preparation 与 validation

Preparation 应默认离线：不下载依赖、不安装 toolchain、不调用模型。它负责结构提取、proof 隔离、
hash/provenance 和静态审计。Validation 必须能够检测：

- upstream revision、license、source 或 fixture 被修改；
- task 数量、split 或 task id 集发生变化；
- statement/scaffold hash 不一致；
- ground-truth proof 意外残留；
- fixture 不再是合法的单 active-hole task。

准备完成只代表结构有效，不能标记为 checker eligible。

### 4. Eligibility gate

Eligibility 在任何模型运行前、使用 benchmark 固定的真实 Lean 项目完成：

- masked scaffold 能 elaboration；
- ground-truth 回填（若 license 和 adapter 允许）能通过整体 checker 与 safety；
- statement 不引用不应可见的其他 benchmark task；
- capability/toolchain 缺失记录为 infrastructure，不判定命题为假；
- 每题保存 candidate hash、checker command/category、toolchain 和 adapter version。

批量检查可聚合后递归二分，但最终证据必须能定位到单题。复用历史 eligibility 只允许复用
`eligible` 且 materialized candidate SHA-256、toolchain、dependencies 和 checker config 全部一致的
记录；失败和 infrastructure 结果不能复用为成功。

Eligibility 冻结后，所有 preregistered arm 使用相同 task set。不得依据 pilot 或正式运行的成功率
删题或移动 split。

## Live Runner 契约

### 生命周期与隔离

- 一个 suite 运行使用一个 Python 进程并复用 persistent Lean server，避免逐题冷启动。
- proof tool 与正式 checker 复用同一 server；正式 benchmark 禁止静默退回独立 subprocess。
- 每题创建独立 controller、预算、workspace、trace、result 和 cost ledger。
- execution mode 在单题运行内不可变；minimal/structured 不得静默互相退化。
- suite runner 只运行 frozen eligible task，不在 live 阶段重新解释 eligibility。

### 冻结配置与 resume

`run.json` 至少固定：

- suite/revision/split 与有序 task ids；
- adapter、manifest 和 eligibility evidence hash；
- model id、role-specific model、temperature/seed 和 endpoint protocol；
- prompt/tool protocol、execution mode、frontier/action mask、retrieval 配置；
- checker/server timeout、模型 timeout、检查/调用/token/USD 预算；
- frozen price table、cost history snapshot 和 routing 配置；
- proof CLI 参数及 config SHA-256。

Resume 必须验证 config hash；task selection 和 proof 参数不同则拒绝继续。已完成的非基础设施结果默认
跳过；targeted infrastructure retry 只重跑 infrastructure task。重跑追加 trace，当前 `result.json` 可更新，
但不得删除原始 observation。

### Outcome taxonomy 与分母

统一使用下列互斥结果：

1. `accepted`：checker 原始接受且 safety review 通过；多 obligation 还要求 final assembly 重检通过。
2. `proof_failure`：模型已正常完成生成，但候选未通过 checker/safety，或在冻结预算内无 accepted proof。
3. `infrastructure_failure`：provider/transport error、checker/server error、timeout、tool unavailable 或外部依赖故障。

`generation:provider_error` 即使 0 candidate、0 checker，也必须归入 infrastructure。基础设施失败不进入
正式 accepted-rate 分母，应在修复后按原冻结配置重跑。不得把 unknown、timeout 或 capability missing
解释为数学失败。

报告至少同时给出 selected、completed、accepted、proof failures、infrastructure failures、retried 和
missing。`ok: true` 只表示 suite runner 正常完成，不代表全部题目被证明。

## Trace、成本与排障

所有 suite 使用 `JsonlTraceStore` 和统一 `CostLedger`：

- provider request/retry、token usage、tool call、checker、pricing 和 charge 均可追溯；
- input、cached input、visible output、reasoning 和 billed/provider total 分开记录；
- measurement status 区分 observed、estimated、unavailable 与 unbounded；
- 价格只来自 provider charge 或显式冻结价格表；
- trace 默认保留结构化 feedback，必要时由配置决定是否保存 raw checker output。

排障先使用 failure-first reader：

```bash
python scripts/trace_pretty.py path/to/trace.jsonl
```

可直接传 task 目录。常用选项：`--latest`、`--show-proof`、`--show-cost`、`--raw-events`。默认视图先显示
stop reason、generation/provider error、provider retry、tool timing 和 Lean feedback，避免从完整 ledger
中人工定位首要错误。

## Pilot、正式实验与公平性

### 工程预检

- preparation/validation 可复现；eligibility 已冻结；
- masked scaffold 可 elaboration，ground-truth 回填验证通过；
- 每题单 active hole，多洞依赖可 materialize；
- 所有 arm 使用相同 trace schema、ledger、checker/safety 和基础预算定义。

### Live pilot

Pilot 只用于确认 timeout、prompt 长度、usage coverage、tool/server 生命周期、自然 action exposure 和预算
尺度。Pilot 后冻结 task ids、模型版本、temperature/seed、prompt/protocol、价格表、history snapshot 和
controller 配置。不得依据 arm 成功率筛题。

### 正式运行与报告

- task/repetition 之间配对或轮换 arm 顺序；
- 分离 action-space、cost-aware selection 和 cheap/strong routing 的收益；
- action mask baseline 必须共享模型、prompt 信息、retrieval、checker 和预算；
- 按 benchmark-model 单元格和总体分别报告，不只报告 pooled mean；
- 报告 paired comparison、bootstrap CI、accepted-rate non-inferiority、实际 token/USD、同成本成功率、
  measurement coverage 和 infrastructure retry 数量；
- richer-only action 应报告 proposal/execution/acceptance 与独占成功案例。

最终实验规模与判定门槛见 [`development-roadmap.md`](development-roadmap.md)。

## 当前 miniF2F 实例

### 实现模块边界

- `agent/benchmarks/minif2f.py`：兼容入口，以及 prepared suite 写入/校验。
- `agent/benchmarks/minif2f_source.py`：上游源码布局、theorem 提取与 scaffold 构造。
- `agent/benchmarks/minif2f_eligibility.py`：eligibility 运行编排。
- `agent/benchmarks/minif2f_eligibility_support.py`：候选物化、跨题引用审计与 evidence 写入。
- `agent/benchmarks/minif2f_runner.py`：persistent Lean server 生命周期与逐题执行。
- `agent/benchmarks/minif2f_run_report.py`：结果分类、resume 历史、summary 和人类可读索引。

外部调用继续使用原有 `minif2f`、`minif2f_eligibility` 和 `minif2f_runner` 导入路径；辅助模块不是新的
公共入口。

### 外部数据与准备

Google DeepMind Lean 4 miniF2F checkout 位于 `benchmark/miniF2F/`，生成数据位于
`benchmark/generated/miniF2F/`，均不提交 Git。准备 488 个独立单洞任务：

```bash
python scripts/minif2f_prepare.py prepare
python scripts/minif2f_prepare.py validate
```

adapter 固定 244/244 split，排除 `.variants.`，移除少量非 `sorry` 的上游 proof，并记录 statement、
scaffold、source、license、revision、toolchain 和 dependency provenance。准备后的 manifest 初始状态为
`eligibility: not_checked`。

安装 benchmark 固定的 Lean/Lake 环境后运行真实 eligibility：

```bash
python scripts/minif2f_eligibility.py
```

该 gate 以 `sorry` 替换 proof marker，审计跨题标识符引用，按 split 聚合检查并在失败时递归二分到单题。
结果保存在 `benchmark/generated/miniF2F/eligibility_runs/`。`--reuse-results <prior-results.jsonl>` 只复用
candidate SHA-256 未变化的 eligible 证据。

### Pilot 与正式执行

```bash
python scripts/minif2f_benchmark_run.py \
  --split valid --limit 5 --run-name minif2f-valid-pilot \
  --execution-mode minimal \
  -- --use-model --max-model-calls 3 --max-checks 3 \
  --lean-timeout 300 --lean-server-startup-timeout 300
```

普通 prove 参数放在 `--` 后；suite selection 和输出参数放在 `--` 前。benchmark 默认使用
`--execution-mode minimal`；显式改为 `structured` 可运行结构化搜索消融。冻结配置后移除 `--limit`
运行完整 valid split，之后才运行 test split。

Resume 必须保留原 selection 和 proof 参数：

```bash
python scripts/minif2f_benchmark_run.py \
  --split valid --limit 5 \
  --resume .runs/benchmarks/minif2f/minif2f-valid-pilot \
  -- --use-model --max-model-calls 3 --max-checks 3 \
  --lean-timeout 300 --lean-server-startup-timeout 300
```

普通 resume 会自动重跑 provider/checker 等 infrastructure result，以及
`generation:model_output_truncated` 这类可恢复的生成失败；它仍会跳过已有 accepted 和普通证明失败。
若需要保留这些结果，可显式传 `--skip-infrastructure-failures` 或
`--skip-transient-generation-failures`。兼容的显式写法为：

```bash
python scripts/minif2f_benchmark_run.py \
  --split valid --limit 5 \
  --resume .runs/benchmarks/minif2f/minif2f-valid-pilot \
  --retry-infrastructure-failures \
  -- --use-model --max-model-calls 3 --max-checks 3 \
  --lean-timeout 300 --lean-server-startup-timeout 300
```

`summary.json` 每完成一题原子更新；运行根目录的 `README.md` 和 `task-index.csv` 每 10 题及运行结束时
刷新，避免 Windows 上的重复全量扫描和频繁 `fsync`。前者只展示当前失败与 pending 任务，后者提供
全部任务的可排序状态索引；历史失败仍单独保存在 `summary.json#error_history`。
对于更新前创建或仍由旧进程执行的 run，可随时无副作用地刷新索引：

```bash
python scripts/minif2f_benchmark_run.py \
  --refresh-index .runs/benchmarks/minif2f/minif2f-valid-pilot
```

## 历史兼容入口与 action arms

- `phase8_benchmark_*` 保留原 trace/replay backend 名称。
- `phase10_benchmark_*` 保留内部 controlled/live canary 名称。
- 文件名、trace layout、serialized arm id 和旧 estimator version 是持久化兼容面，不代表当前阶段。
- `.runs/phase8/stage1-canary` 和 controlled simulated cost 不能进入正式 accepted-rate/成本报告。

历史 live arms 对应显式 runtime 配置：

- `A2`：static action costs，关闭 remaining-budget admission；
- `A3`：frozen empirical costs，关闭 remaining-budget admission；
- `A4`：frozen empirical costs，开启 remaining-budget admission；
- `A5`：`A4` 加 cheap/strong routing；
- `A6`：`A4` 使用单一 cheap model 并关闭 routing。

Empirical arms 必须提供 `--cost-history-snapshot`。trace 中的实际 `action_runtime_config` 才是消融执行
证据，provenance label 本身不构成证据。

## 新 Suite 合入清单

- [ ] source/revision/license/toolchain/dependencies 已冻结并可验证；
- [ ] extraction、proof isolation、split、hash 和 round-trip 测试已覆盖；
- [ ] preparation 与真实 eligibility 明确分离；
- [ ] eligibility 在模型运行前冻结，且无跨题泄漏；
- [ ] runner 复用 required persistent server，proof tool 不另起 checker；
- [ ] per-task budget/workspace/result/trace/ledger 隔离；
- [ ] provider/checker/timeout 分类为 infrastructure，可 targeted retry；
- [ ] config hash 阻止不同配置 resume；
- [ ] ground truth 不进入 prompt/retrieval/失败上下文；
- [ ] action masks、models、budgets 和 task order 满足公平比较；
- [ ] trace pretty reader 能定位 provider、tool 和 Lean error；
- [ ] 文档只更新本文与 roadmap 状态，不在 `scripts/` 新建 Markdown。
