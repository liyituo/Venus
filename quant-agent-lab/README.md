# Quant Agent Lab

`quant-agent-lab` 是一个隔离、离线优先的金融决策与 Paper Trading 工程演示。它支持本地 JSON 快照或 **live 行情拉取**（A 股 akshare / 美股 yfinance），经策略生成可审计的计划和风险检查，要求人类批准后才允许确定性 `PaperBroker` 执行，并提供一个可通过 MCP Apps 嵌入宿主 Chat 的极客终端风格交互式仪表盘。新增的 K 线与 Strategy Lab 仍只调用后端权威结果，不在浏览器生成交易事实。

默认策略为 **Tiny-MoE 横截面排序**（`tiny-moe-ranker@2.0.0`），使用仓库内自带的 `A4_tiny_moe_v2` 权重对 CSI300 成分股日级排序；也可切换回 `llm-fundamental` 或 `moving-average-demo`。

这不是投资建议，不做收益承诺，不连接券商账户、凭据或真实资金。`LiveBroker` 永久禁用；任何非 `paper` 执行都会安全失败。

## 目录

```text
quant-agent-lab/
├─ config/                         # demo/risk 版本化配置；csi300_symbols.txt
├─ contracts/v1/                   # OpenAPI、JSON schema、错误码
├─ docs/                           # 后端调试、GUI、嵌入、安全边界、研究合同
├─ plugins/quant-agent-dashboard/  # MCP server、MCP App UI、harness
├─ scripts/                        # 辅助脚本（如 run_cached_report.py）
├─ artifacts/gui/                  # 浏览器视觉检查截图
├─ src/quant_agent/                # 共享 ApplicationService 后端
│  ├─ ml/                          # Tiny-MoE 推理（feature_builder / predictor）
│  └─ strategies/                  # tiny_moe_ranker / llm_fundamental / ...
├─ tests/                          # unit/integration/contract
└─ var/
   ├─ models/A4_tiny_moe_v2/       # 模型权重与 metrics（已入库）
   ├─ data/                         # 账户/行情缓存（Git ignored）
   ├─ reports/                      # 日报 JSON/MD（Git ignored）
   └─ audit/                        # 审计日志（Git ignored）
```

父 Agent 不在 Python import path 中；所有源代码、运行时状态和 GUI 资产都留在本目录。训练源码见仓库根目录 `量化模型/tiny_moe_quant/`。

## 后端启动

在 PowerShell 项目根目录：

```powershell
python -m pip install -e ".[dev,ml,live-data]"
python -m quant_agent init-db
```

**Paper 演示（离线合成行情）**：

```powershell
python -m quant_agent seed-demo --reset
python -m quant_agent demo --date 2026-08-11
```

**Tiny-MoE + A 股 live 行情（默认 `config/demo.yaml`）**：

```powershell
# 确保 var/data/account_snapshot.json 为 CNY 纸面账户（currency: CNY，初始现金自定）
python -m quant_agent generate-report
python -m quant_agent approve REPORT_ID --all --approver user
python -m quant_agent execute REPORT_ID --paper
python -m quant_agent status REPORT_ID
```

说明：

- `market_data.source: live` 时从 akshare（新浪优先）拉取 `config/csi300_symbols.txt` 中 300 只成分股日线；失败回退 `var/data/market_snapshot.json` 缓存。
- 模型权重：`var/models/A4_tiny_moe_v2/best_model.pt`（约 130 KB，CPU 推理即可）。
- 横截面不足 50 只或模型加载失败时，策略降级为全 HOLD，不伪造信号。
- 周末/节假日行情可能被判「过期」，可在 `config/risk.demo.yaml` 调整 `max_data_age_seconds`。

切换策略：编辑 `config/demo.yaml` 中 `strategy.id`（`tiny-moe-ranker` | `llm-fundamental` | `moving-average-demo`）。

开发者运行完整测试：

```powershell
python -m pip install -e ".[dev,ml,live-data]"
pytest -q
```

生成报告并显式批准/执行（通用 CLI）：

```powershell
python -m quant_agent generate-report --date 2026-08-11
python -m quant_agent approve REPORT_ID --all
python -m quant_agent execute REPORT_ID --paper
python -m quant_agent status REPORT_ID
```

也支持精确的部分批准、报告查看、拒绝和本地 Kill Switch：

```powershell
python -m quant_agent approve REPORT_ID --order-id ORDER_ID --approver user
python -m quant_agent report REPORT_ID
python -m quant_agent reject REPORT_ID
python -m quant_agent kill-switch --on --reason "operator stop"
python -m quant_agent kill-switch --off
python -m quant_agent demo --date 2026-08-11
```

API 使用同一个 `ApplicationService`，没有复制业务逻辑：

```powershell
$env:PYTHONPATH = 'src'
uvicorn quant_agent.api.app:app --host 127.0.0.1 --port 8014
```

主要路由：`/api/v1/health`、`/api/v1/dashboard`、`/api/v1/audit`、`/api/v1/daily-plans`、报告/审批/拒绝/执行、执行查询和 `/api/v1/kill-switch`。错误返回稳定的 `code/error/message`；mutating 请求支持 `request_id`。

研究接口位于 `/api/v2`：`chart-data`、策略注册/校验/草稿、DebugTrace、确定性回测、回测比较、Paper Candidate 提升和显式 Paper Strategy 启用。默认快照为 1d 本地合成数据，API 中价格和数量使用十进制字符串；当前环境没有可信 Python 代码沙箱，因此任意 Python 策略始终返回 `SANDBOX_UNAVAILABLE`。

## MCP Apps GUI

插件目录：`plugins\quant-agent-dashboard`。

```powershell
cd plugins\quant-agent-dashboard
node scripts\build.mjs
node standalone\server.mjs
```

正式本地入口是 `http://127.0.0.1:4173/#/dashboard`，它连接运行在 `127.0.0.1:8014` 的真实量化后端，并复用 MCP Apps 的 UI 资产和结构化工具合同。主 Agent 的「量化中心」按钮会自动完成健康检查、按需启动和导航，且不会触发报告生成、审批或交易。

开发和异常场景测试仍可使用本地 harness：

```powershell
node harness\server.mjs
```

Harness 会嵌入构建后的 UI，并模拟 `ui/initialize`、tool input/result、`tools/call`、`ui/message` 和主题/错误状态。可用 URL 参数检查 `?scenario=blocked|partial|kill-switch|error|expired|conflict&theme=dark`。

MCP server 暴露交易工具：

`quant_get_dashboard`、`quant_generate_daily_plan`、`quant_get_report`、`quant_submit_approval`、`quant_reject_plan`、`quant_execute_paper_plan`、`quant_set_kill_switch`、`quant_get_execution`、`quant_get_audit_events`。

研究工具：

`quant_get_chart_data`、`quant_list_strategies`、`quant_get_strategy`、`quant_validate_strategy`、`quant_save_strategy_draft`、`quant_run_strategy_debug`、`quant_get_debug_trace`、`quant_run_backtest`、`quant_get_backtest_result`、`quant_compare_backtests`、`quant_promote_strategy_candidate`、`quant_enable_paper_strategy`。

Strategy Lab 使用显式 JSON AST DSL，允许字段、指标、运算符、参数范围和资源上限均由后端校验；保存草稿不会替换 `moving-average-demo@1.0.0`，也不会产生订单。回测信号在收盘后产生，最早下一根 K 线开盘成交，并在结果中记录手续费、滑点、资金约束和公式假设。完整合同见 [docs/chart-data-contract.md](docs/chart-data-contract.md)、[docs/strategy-lab.md](docs/strategy-lab.md)、[docs/backtest-methodology.md](docs/backtest-methodology.md)、[docs/custom-strategy-security.md](docs/custom-strategy-security.md) 和 [docs/strategy-debugging.md](docs/strategy-debugging.md)。

UI 通过 `_meta.ui.resourceUri = ui://quant-agent-dashboard/dashboard.html` 提供 MCP App resource，使用共享 MCP Apps bridge，不依赖 `window.openai` 专用接口；每个副作用操作成功后都会重新读取后端权威 dashboard。真实 Chat 宿主的认证和正式嵌入验收尚未声称完成，见 [docs/chat-embedding.md](docs/chat-embedding.md)。

## 验证

```powershell
$env:PYTHONPATH = 'src'
python -m compileall -q src
pytest -q
ruff check src tests
ruff format --check src tests
mypy src

cd plugins\quant-agent-dashboard
node --test mcp-server/tests/contract.test.mjs ui/tests/ui-contract.test.mjs standalone/tests/host-contract.test.mjs
node harness\e2e-demo.mjs
python <plugin-creator-skill>\scripts\validate_plugin.py `
  .\plugins\quant-agent-dashboard
```

当前验证基线：后端 **50+ passed**（含 Tiny-MoE / live provider 单测），MCP/UI/standalone Node 合同 `5 passed`，插件结构校验和主 Agent 到真实本地服务的联调均通过。Harness 使用系统时钟；当固定离线快照超过新鲜度窗口时，审批会按设计返回 `RISK_BLOCKED`，不得为演示绕过风控。视觉检查截图在 [artifacts/gui](artifacts/gui)；调试根因、架构和安全约束分别见 [docs/debug-report.md](docs/debug-report.md)、[docs/gui-architecture.md](docs/gui-architecture.md)、[docs/ui-security.md](docs/ui-security.md)。远程 MCP 注册与真实资金交易不在本项目范围内。

## Tiny-MoE 策略说明

| 项目 | 值 |
|------|-----|
| 策略 ID | `tiny-moe-ranker@2.0.0` |
| 模型 | A4 Tiny-MoE V2（约 2.9 万参数） |
| universe | CSI300 当前成分（300 只，存在幸存者偏差） |
| 信号 | Top-20 BUY / 尾部 SELL，其余 HOLD |
| 测试集 RankIC（2023–2024） | 0.034 |
| 推理设备 | CPU（`tiny_moe.device: cpu`） |

模型回测结果见 `var/models/A4_tiny_moe_v2/metrics.json`；**历史回测不代表未来收益**。完整训练、评估与消融见 `量化模型/tiny_moe_quant/README.md`。
