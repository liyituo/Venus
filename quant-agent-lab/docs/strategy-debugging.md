# Strategy debugging

`quant_run_strategy_debug` 在固定 snapshot、固定策略版本和固定参数上生成可重放的 `run_id`。每个 trace bar 包含：

- bar index、timestamp、OHLCV；
- 每个指标值；
- 每条规则的 true/false 和解释细节；
- BUY/SELL/HOLD、reason_code；
- warmup 标识；
- 是否产生候选交易；
- 没有信号时的原因。

GUI 支持首根/上一步/下一步/末根导航，点击 K 线时显示对应 OHLCV 和指标，结果不会影响账户、报告、审批或 PaperBroker。

策略状态、DebugTrace 和回测结果分别存放在 `var/strategies`、`var/research/debug`、`var/research/backtests`，均位于隔离项目内。
