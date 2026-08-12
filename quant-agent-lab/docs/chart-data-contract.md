# Chart data contract

图表接口是研究只读接口，不是交易状态接口：

```text
POST /api/v2/chart-data
```

请求至少包含 `symbol` 和 `timeframe`，可选 `strategy_id`、`version`、`snapshot_id`、`start`、`end`、`max_bars` 与 `report_id`。

响应 `schema_version=chart-data.v2` 包含：

- `bars`: 带时区的 OHLCV；价格和数量以十进制字符串传输；每根 K 线带 `timeframe`、`source`、`is_synthetic`、`session` 与 `snapshot_id`。
- `indicators`: 当前策略声明的指标线；浏览器不计算权威信号。
- `signals`: BUY/SELL、时间、价格、reason_code 和 strategy source hash。
- `markers`: 候选订单、批准订单、PaperBroker 成交和持仓成本线。
- `snapshot_id`、`data_source`、`data_as_of`、`data_status`、`stale`、`is_synthetic`。
- `supported_timeframes`：由当前快照真实提供的周期；本地 demo 当前只有 `1d`。

后端拒绝不支持的周期、缺失标的、重复/逆序时间戳和非当前 snapshot。不会补造缺失 K 线，也不会使用未来 K 线。

图表 UI 使用本地构建的 SVG renderer，不依赖 CDN 或第三方运行时；缩放、平移窗口、悬停 OHLCV 和十字光标属于展示状态，不会改变后端结果。
