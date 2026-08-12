# Backtest methodology

回测使用固定本地 `MarketSnapshot`，最大 500 根 K 线，确定性 run_id 绑定策略 source hash、参数、快照、标的和交易成本。

核心假设：

1. 当前 K 线收盘后才计算信号。
2. 最早在下一根 K 线开盘执行，买入使用向上滑点，卖出使用向下滑点。
3. 只允许 long-only；不使用杠杆、保证金或裸卖空。
4. 现金不足时按整数数量向下取整。
5. 费用和滑点以 basis points 确定性计算。
6. 样本不足时波动率、Sharpe 等指标显示 `N/A`，不伪造年化值。

结果包含总收益、基准收益、最大回撤、波动率、Sharpe、胜率、盈亏比、profit factor、换手率、费用、滑点、终值、净值序列、回撤序列和交易记录，并同时保存公式与假设。

策略研究运行不会调用 `PaperBroker`，也不会写入交易账户、审批或主审计流；研究事件写入 `var/research/events.jsonl`。
