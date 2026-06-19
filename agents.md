# Agent 项目说明

## 运行规则

- Lean smoke / 真实 checker：需要时直接申请非沙箱运行，尤其是 elan toolchain 相关命令。
- 不读取 `.env` 内容，只检查存在性和用脚本消费它，避免把 key 打出来。

## 项目目标

本项目是一个面向 Lean 4 的成本敏感证明搜索（cost-sensitive proof search）agent 框架。核心流程：

1. 接收自然语言数学问题或已有 Lean 模板。
2. **Formalizer** 把自然语言形式化为带有一个证明洞（hole marker）的 Lean scaffold。
3. **ProofController** 在预算限制下循环生成候选证明、调用 Lean checker、根据反馈修复。
4. 返回可被 Lean 接受的完整证明或失败报告。

## 模块结构

```
agent/
├── agents/              # 各 agent 角色与共享模型基础设施
│   ├── chat_driver.py   # 通用 chat-completion 驱动（payload、tool loop）
│   ├── openai.py        # ChatConfig / ChatTransport / OpenAI 兼容 HTTP 层
│   ├── tools.py         # Tool 协议、LeanEnvironmentToolProvider、run_tool_loop
│   ├── formalization.py # Formalizer：自然语言 -> Lean scaffold
│   ├── proof.py         # Proof generator：补全 proof hole
│   └── config.py        # AgentRole / RoleModelConfig
├── search/              # 搜索控制
│   ├── action.py        # ActionGenerator 协议与 ActionCandidate
│   ├── controller.py    # ProofController 主循环
│   ├── budget.py        # 预算管理（checks / model calls / time）
│   ├── proposer.py      # 候选库生成
│   └── state_encoder.py # 证明状态编码
├── proof_system/        # Lean 适配层
│   ├── lean.py          # LeanAdapter 编排器
│   ├── lean_project.py  # Lake 项目检测
│   ├── lean_command.py  # lean / lake 命令行构建
│   ├── lean_subprocess.py # 子进程执行与进程树清理
│   ├── lean_feedback.py # Lean 诊断输出解析
│   ├── lean_server.py   # 持久化 Lean server
│   └── base.py          # ProofSystemAdapter / ProofTask / CheckResult
├── retrieval/           # 检索（当前为词法检索 LexicalLeanRetriever）
├── input/               # 输入解析、规范化、scaffold 校验
├── tasks/               # 任务构建
├── runtime/             # workspace、trace store、logging、env loader
└── cli/                 # 命令行入口 solve_lean_task.py
```

## Agent 角色

| 角色 | 类 | 输入 | 输出 | 说明 |
|---|---|---|---|---|
| Formalizer | `ChatFormalizationAgent` | `FormalizationRequest` | `FormalizationResult` | 生成含 `{{proof}}` 的 Lean scaffold，可接 checker 做校验重试。 |
| Proof Generator | `ChatActionGenerator` | `ActionGenerationRequest` | `Sequence[ActionCandidate]` | 根据当前 proof state 生成候选证明体。 |

> 旧名保留兼容别名：`OpenAIChatConfig`、`OpenAIChatFormalizationAgent`、`OpenAIChatActionGenerator`。

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
```

后续可按 role 读取不同环境变量（如 `OPENAI_MODEL_FORMALIZER`、`OPENAI_MODEL_PROOF_GENERATOR`）实现“便宜模型 tactic + 贵模型 prove”。

## 关键边界协议

- `FormalizationAgent`：形式化入口。
- `ActionGenerator`：证明候选生成入口。
- `ProofSystemAdapter` / `LeanAdapter`：Lean checker 适配。
- `Retriever`：检索接口（`ProofController` 使用）。
- `ScaffoldChecker`：scaffold 校验接口。

## CLI 入口

```bash
python -m agent.cli.solve_lean_task <source> --use-model
```

常用选项：`--use-model`、`--candidate`、`--max-checks`、`--max-model-calls`、`--enable-retrieval`。

## 测试

```bash
python -m pytest tests/ -q
```

重点测试：

- `tests/test_formalization.py`：formalizer 校验、重试、缓存、tool 调用。
- `tests/test_model_adapter.py`：proof generator、transport 重试。
- `tests/test_chat_driver.py`：新抽象层单测。
- `tests/test_controller.py`：ProofController 主循环。

## 注意事项

- `ChatConfig` 是 OpenAI 兼容配置，可用于任意 OpenAI-compatible endpoint。
- 真实 Lean checker 调用可能耗时较长，CLI/测试支持超时与重试。
- `.env` 只由脚本消费，不要直接读取其内容写入日志。
