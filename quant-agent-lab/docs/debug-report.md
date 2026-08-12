# Stage A 调试报告

## 范围与隔离

本报告记录 `quant-agent-lab/` 隔离开发阶段的后端、版本化 API、PaperBroker、MCP 适配器和本地 UI harness。该阶段没有导入或修改父 Agent；后续主 GUI 接入通过仓库根目录的薄控制器完成，量化项目仍没有嵌套 `.git`。所有运行时数据库、离线快照、报告和审计日志都在项目的 `var/` 下。

`LiveBroker` 仍是显式拒绝适配器；CLI 只接受 `--paper`，API 和 MCP 工具没有 live 模式或凭据参数。本轮没有真实券商连接、网络行情、账户凭据、通知或真实资金操作。

## 基线证据

本轮开始时项目 Git 状态为 `main...origin/main [ahead 1]`，只有既有 `.pytest_cache` 权限警告；未发现嵌套 Git。环境探测结果：Python `3.13.15`、Node `v24.11.1`、npm `11.6.2`（PowerShell 的 `npm.ps1` 受执行策略限制，使用 `npm.cmd`）、pnpm `11.16.0`，Yarn 不可用。项目没有本地 React/Vite/Playwright 依赖；`npx --no-install playwright --version` 会尝试读取 registry 并因本机缓存 EPERM 失败，本轮没有安装依赖。视觉检查使用已连接的本地浏览器能力和项目内 harness。

开始时的后端基线命令：

| 命令 | 结果 |
|---|---|
| `$env:PYTHONPATH='src'; python -m compileall -q src` | exit 0 |
| `$env:PYTHONPATH='src'; pytest -q` | 17 passed |
| `ruff check src tests` | All checks passed |
| `ruff format --check src tests` | 47 files already formatted |
| `mypy src` | no issues, 43 source files |
| `$env:PYTHONPATH='src'; python -m quant_agent --help` | CLI help exit 0 |
| `$env:PYTHONPATH='src'; python -m quant_agent demo --date 2026-08-11` | PaperBroker `FILLED`，无 live 路径 |

## 缺陷、根因与修复

| 优先级 | 症状 / 最小复现 | 根因 | 修复与回归证据 |
|---|---|---|---|
| P1 | 将市场 JSON 的 `as_of` 写成无时区值后，报告生成抛原始 aware-naive `TypeError` | `validate_market` 记录了时区问题后仍做时间相减 | `DATA_TIMEZONE_MISSING` 结构化阻断；异常值原样可序列化，不补 UTC；`test_timezone_missing_market_data_is_a_structured_block` |
| P1 | 相同审计事件在 SQLite 只有一条，JSONL 却有两条 | `INSERT OR IGNORE` 后无条件追加 JSONL | 依据 cursor `rowcount` 仅在新插入时镜像；`test_duplicate_audit_event_is_mirrored_once` |
| P1 | 账户快照陈旧两天仍进入 `PENDING_APPROVAL` | 账户校验只有余额、币种、状态和一致性检查 | 账户复用市场 freshness policy，加入 `ACCOUNT_STALE`/`ACCOUNT_TIME_IN_FUTURE`，且执行前再次校验；`test_stale_account_is_blocked_and_cannot_be_approved`、`test_execution_reloads_and_blocks_stale_account` |
| P1 | 审批后恶意/损坏市场文件含无时区时间，执行风险层仍抛 `TypeError` | `RiskEngine.evaluate` 无条件计算市场年龄 | freshness check 在风险层也先验证时区；`test_risk_engine_blocks_timezone_invalid_market_without_type_error` |
| P2 | API 缺失报告返回 409 字符串，无法稳定消费；没有 dashboard/audit 视图 | `_call` 只暴露异常类名，版本化 API 没有 GUI 投影路由 | 增加 `code/error/message` 错误包、`REPORT_NOT_FOUND` 404、`GET /api/v1/dashboard`、`GET /api/v1/audit`，并为 mutating request 传递 `request_id`；API 合同测试覆盖 |
| P1 | MCP harness iframe 空白 | UI HTML 的 `frame-ancestors 'none'` 拒绝宿主嵌入 | 改为允许宿主 iframe；MCP Apps 资源仍由宿主 sandbox/CSP 控制，UI 自身无外部连接；浏览器重载后可见完整界面 |
| P2 | 390px 视口页面级横向滚动到 766px | report ID 的 flex/grid item 使用默认 `min-width:auto` | `.content > *`、`.report-strip` 子项允许收缩；浏览器检查 `scrollWidth === clientWidth`，订单表只在自己的表容器内滚动 |
| P2 | Windows 下 `node --test mcp-server/tests ui/tests` 把目录当模块加载失败 | Node 24 的当前 CLI 不自动展开目录参数 | package script 改为显式测试文件；MCP/UI 合同测试 4/4 |

## Stage A 最终验证

最终后端测试为 `23 passed in 2.80s`，并通过：

```text
ruff check src tests                 -> All checks passed
ruff format --check src tests        -> 47 files already formatted
mypy src                              -> no issues in 43 source files
python -m compileall -q src          -> exit 0
```

CLI 离线演示仍是 `seed-demo -> generate-report -> explicit approval -> --paper execute`，最终状态 `FILLED`。报告 JSON 与 Markdown 均由同一 `DailyReport` 写出；金额为 Decimal 字符串，时间为 UTC RFC 3339。审批绑定报告版本、计划哈希、账户、策略、风控版本和精确订单集合；计划变更、过期、拒绝、风险阻断、Kill Switch 和执行前复核都会阻断或要求重新确认。重复执行返回同一执行结果，不重复调用 PaperBroker。

## Stage B 证据

- MCP server/UI 插件目录：`plugins/quant-agent-dashboard/`。
- MCP 合同和 UI 合同：Node test `4 passed`。
- `python ...validate_plugin.py ...\plugins\quant-agent-dashboard`：`Plugin validation passed`。
- 本地 harness：`http://127.0.0.1:4173/`，能够模拟 `ui/initialize`、tool input/result、`tools/call`、`ui/message`、主题切换、断开、过期和版本冲突。
- 端到端模拟演示：生成 `rpt_2026-08-11_0cbd55b717d5692c`，批准 `appr_6cd83db652d90c8d9338`，执行 `exec_6bf70d6af66a752bf79a`，`execution_status=FILLED`、`mode=paper`、`paper_only=true`、`live_broker=disabled`。
- 浏览器交互确认：批准后界面显示“批准不会自动执行”，只有再次点击并确认才发送 `quant_execute_paper_plan`；成功后界面立即重新读取 dashboard 权威状态。
- 截图在 `artifacts/gui/`，覆盖 Light/Dark 桌面、移动、阻断、确认、部分成交、Kill Switch 和错误状态。

## Incremental research upgrade evidence

- Research regression: `pytest -q` -> `29 passed`; `ruff check src tests`,
  `ruff format --check src tests`, `mypy src`, and `python -m compileall -q src`
  all succeeded. The Node MCP/UI/standalone contract is `5 passed` and the plugin
  validator reports `Plugin validation passed`.
- `moving-average-demo@1.0.0` remains registered and is rendered from the
  backend chart contract with 40 deterministic 1d synthetic bars, Decimal-string
  values, indicator series, backend signals, and the `SIMULATED DATA` snapshot
  badge. The first research screenshots are
  `artifacts/gui/candlestick-overview.png` and
  `artifacts/gui/candlestick-signals.png`.
- The visual pass found and fixed two concrete GUI defects: an extra array layer
  made the three view-tab buttons inert, and JavaScript `Number(null)` converted
  indicator warm-up values into zero and flattened the K-line price axis.
- The local browser auto-review subsequently denied additional access to the
  Harness URL. Therefore the ten new research screenshot names required by the
  upgrade were not claimed as complete; Strategy Lab, mobile, DebugTrace,
  backtest, comparison, Paper Candidate, and sandbox-unavailable captures remain
  a follow-up that needs permitted local-browser access. No alternate browser
  path or raw CDP workaround was used.

真实 Chat 宿主的认证、远程 MCP 配置和正式宿主验收未在本地环境验证；详见 [chat-embedding.md](chat-embedding.md)。
