# LLM 基本面信号（llm-fundamental）决策记录

## 目标

让 DeepSeek 每天读取 RAG 中的财报 + 结合行情数据，输出日级交易信号（BUY/SELL/HOLD），
替换/并行于现有的均线策略。LLM 只做决策层，下游风险/审批/执行/审计完全复用。

## 数据流

```
每日财报(txt/md) → RAG ingest(8010) → financial-reports 集合（meta: symbol/report_date）
行情自动拉取 → LiveMarketDataProvider（美股 yfinance / A股 akshare）
                                    ↓ 缓存 var/data/market_snapshot.json（失败回退）
LlmFundamentalStrategy：行情上下文 + 财报检索(top_k=5) → DeepSeek 结构化 JSON → StrategySignal
```

## 关键决策

1. **LLM 只做决策，不绕过安全链**：信号输出收敛到现有 StrategySignal 合同
   （BUY/SELL/HOLD + reason_code + strength + invalidation_conditions），
   planner→risk→approval→execution→audit 全部复用，不做任何 LLM 直连执行。

2. **失败降级不伪造信号**：LLM 未配置/不可达/输出非法 → HOLD + 明确 reason_code
   （LLM_NOT_CONFIGURED / LLM_UNAVAILABLE / LLM_BAD_OUTPUT），绝不回退均线冒充。

3. **防未来函数**：检索到的财报片段按 report_date ≤ market.as_of 过滤，
   未来日期的财报不得进入 prompt。

4. **密钥约定**：key 只从环境变量 QUANT_AGENT_LLM_API_KEY 读取（.env，已 gitignore），
   绝不进配置文件/日志/审计。审计摘要截断 500 字符且经"审计不含密钥"测试约束。

5. **行情缓存回退**：拉取成功落盘 market_snapshot.json（与 file 模式同格式）；
   拉取失败回退上次缓存；无缓存则明确报错。

6. **频率**：日级信号（财报与日线数据的自然频率）。分钟级实时信号不在本期范围。

7. **依赖 lazy import**：yfinance/akshare 只在 live 模式加载；未安装时 file 模式
   与 MA 策略完全不受影响。

## 配置

```yaml
# config/demo.yaml
strategy:
  id: llm-fundamental       # 切换为 LLM 策略（默认 moving-average-demo）
llm:
  api_url: "https://api.deepseek.com"    # normalize 由调用方处理（支持 base/v1/完整路径）
  model: "deepseek-v4-flash"
  api_key_env: QUANT_AGENT_LLM_API_KEY
  timeout: 60
  rag_url: http://127.0.0.1:8010
  rag_collection: financial-reports
market_data:
  source: live              # file（默认）| live
  market: both              # us | cn | both
  symbols: [AAPL, MSFT, 600519]
```

安装行情依赖：`pip install -e ".[live-data]"`（yfinance + akshare + pandas）。

## 已知边界

- yfinance 依赖 Yahoo 接口可达性（可能需要代理）；akshare 接口稳定性——均缓存兜底
- 财报内容质量决定信号质量：RAG 检索只取 top_k 片段，超长财报需人工提炼要点
- LLM 信号不保证盈利；系统只保证决策可追溯、安全链不绕过、失败不伪造
