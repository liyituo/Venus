# Custom strategy security

## 声明式 DSL

用户策略只接受 JSON AST，不接受可执行代码。校验器使用允许列表、显式类型和参数边界：

- 字段：`open`、`high`、`low`、`close`、`volume`；
- 指标：SMA、EMA、RSI、MACD、Bollinger、rolling high/low、returns；
- 运算：比较、crossover、crossunder、and、or、not；
- 输出：BUY、SELL、HOLD、strength、reason_code。

同时限制指标数、规则数、规则深度、窗口范围和回测数据量。DSL 不能访问文件、环境变量、网络、Broker 或动态函数名；源码中没有 `eval` / `exec` 执行路径。

## Python 策略

任意 Python 只有在独立进程或容器、无网络、只读输入、无密钥、无主数据库权限、CPU/内存/超时/输出限制都可证明时才允许启用。当前环境不满足证明条件，因此 Python runner 永久安全失败并返回 `SANDBOX_UNAVAILABLE`。

调试与回测环境和 Paper Trading 环境分离；研究运行不得产生 PaperBroker 订单。
