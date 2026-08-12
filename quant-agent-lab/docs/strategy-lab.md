# Strategy Lab

Strategy Lab 是研究环境，与日报、审批和 PaperBroker 分离。默认注册 `moving-average-demo@1.0.0`，它保留原有日报策略行为，同时以声明式 DSL 形式出现在注册表中。

## 状态机

```text
DRAFT → VALIDATED → BACKTESTED → PAPER_CANDIDATE → PAPER_ENABLED
```

每次参数或 DSL 内容变化都会重新计算 `source_hash`。Paper Candidate 固化策略版本、source hash、回测 run、snapshot、参数、回测假设和风险兼容性。启用 Paper Strategy 会创建新版本，不替换当前日报策略，也不继承旧报告审批。

## API / MCP

- `quant_list_strategies` / `quant_get_strategy`
- `quant_validate_strategy` / `quant_save_strategy_draft`
- `quant_run_strategy_debug` / `quant_get_debug_trace`
- `quant_run_backtest` / `quant_get_backtest_result` / `quant_compare_backtests`
- `quant_promote_strategy_candidate` / `quant_enable_paper_strategy`

所有工具即使没有 GUI 也返回结构化结果和稳定 reason code；带副作用的请求要求 `request_id`。

## 当前 Python 状态

当前环境没有可证明的独立进程/容器沙箱，因此 `PythonStrategyRunner` 不执行任意 Python，返回 `SANDBOX_UNAVAILABLE`。声明式策略是完整可用路径。
