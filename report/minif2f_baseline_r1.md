检查完了。结论是：minimal 在 miniF2F valid 上最终为 **244/244，100% accepted**，但这是经过 resume 恢复后的结果，不能简单写成“首次运行 100%”。

### valid 运行情况

运行配置：`deepseek-v4-flash`、temperature 0、每题最多 3 次模型调用和 3 次检查。

| 指标 | 结果 |
|---|---:|
| 最终 accepted | **244 / 244（100%）** |
| 第一次运行即 accepted | 231 / 244（94.67%） |
| 首轮排除 10 个 provider infrastructure failure | 231 / 234（98.72%） |
| 需要 resume 的题 | 13 |
| 历史失败会话 | 22 |
| 其中 provider error | 13 |
| 其中输出截断 | 9 |
| 最终普通 proof failure | **0** |
| 最终 infrastructure failure | **0** |

最终成功会话中：

- 236 题第一次 checker 就通过，8 题第二次通过。
- 222 题只用 1 次模型调用，19 题用 2 次，3 题用满 3 次。
- 因此 minimal 的证明能力确实已经非常高；所有遗留失败都由 provider 故障或输出截断造成，并非 Lean 证明搜索失败。

### Token 实际上可以恢复

报告接口没有汇总 token，但每题 `trace.jsonl` 的 `run_summary.metadata.model_usage` 保留了 usage。

仅统计每题最终 accepted 会话：

| Token 口径 | 总量 | 每题平均 | 中位数 | P95 |
|---|---:|---:|---:|---:|
| Input | 712,141 | 2,919 | 1,774 | 8,150 |
| Visible output | 129,524 | 531 | 234 | 1,705 |
| Reasoning | 1,395,846 | 5,721 | 2,355 | 21,189 |
| Provider total | **2,237,511** | **9,170** | **4,737** | **29,966** |

把所有 resume 和失败会话也计入，实际运行历史中收到 usage 的总量为：

- Input：893,687
- Visible output：135,351
- Reasoning：2,203,710
- Provider total：**3,232,748**
- 平均每道 selected task：13,249 provider tokens
- 中位数：4,800；P95：40,601

这里的 `provider total = input + visible output + reasoning`，cached input 221,440 是 input 的子集，不能再次相加。