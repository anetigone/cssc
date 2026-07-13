# CLAUDE.md — 工作导航

本文件只提供会话导航和工作纪律。当前架构、运行约束和公共协议见 [`agents.md`](agents.md)；阶段历史、当前里程碑与后续评测计划见 [`docs/development-roadmap.md`](docs/development-roadmap.md)。不要在本文件重复维护阶段清单。

## 禁止递归读取的目录

| 路径 | 原因 |
|---|---|
| `lean_workspace/`、尤其 `.lake/` | 大型 Lean/Mathlib 构建树 |
| `.runs/` | 大型 trace；只读用户明确指定的运行 |
| `__pycache__/`、`.pytest_cache/`、`*.pyc` | 缓存产物 |
| `.env` | 含密钥；只允许脚本消费 |
| `.git/` | 不作为源码搜索目标 |

源码搜索限定于 `agent/`、`tests/`、`docs/`、`scripts/`、根入口和明确指定的规划文件。

## 开工前阅读顺序

1. 阅读 [`agents.md`](agents.md) 中与任务相关的当前边界。
2. 若任务涉及路线、评测或历史兼容，再阅读 [`docs/development-roadmap.md`](docs/development-roadmap.md)。
3. 只有需要设计依据时，才阅读 [`tmp/plan1.md`](tmp/plan1.md) 或 roadmap 指向的历史计划。
4. 不要为了寻找上下文递归读取整个 `tmp/`。

## 当前工作重点

- 停止扩展人工行为型 benchmark；内部 canary 仅用于 pipeline/controller 回归。
- 完成外部公开 benchmark 接入、任务 eligibility、环境冻结和公平 action-space baseline。
- 统一 minimal/structured 成本观测、价格表和统计报告。
- 保持 checker、safety、reducer、assembly 语义不受成本策略影响。

具体完成状态和验收门槛以 roadmap 为准。

## 工作纪律

1. 先确定改动边界、复用点和验证方式，再编辑。
2. 小改动运行相关测试；跨模块改动再跑全量测试。
3. Lean smoke 或真实 checker 需要时申请非沙箱运行。
4. 保留用户现有工作树修改，不清理无关文件。
5. 子包内优先 1～2 点相对导入；更深路径使用绝对导入。
6. 代码注释和 docstring 描述当前语义，不写“某阶段新增/暂不实现”等迭代日志。
7. 历史 benchmark 文件名和序列化 version 字段属于兼容接口；需要重命名时先设计迁移。

## 提交信息

提交信息描述实际能力或修复，例如：

```text
Unify proof-search cost accounting
Add external benchmark task adapter
```

不要把阶段编号作为新提交信息的必要前缀。
