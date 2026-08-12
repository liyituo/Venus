# UI 与 MCP Apps 安全边界

## 不可越过的边界

- UI 没有 broker、SQLite、文件系统或凭据访问；所有业务决策经后端 ApplicationService。
- UI 只发送结构化 MCP tool intent，不能构造最终 broker order、修改 plan hash、跳过风险检查、绕过审批、关闭 Kill Switch 后自动执行或重放未经确认的执行。
- `quant_execute_paper_plan` 的 schema 没有 mode 参数，Node adapter 固定请求 API `mode=paper`；`LiveBroker` 后端继续拒绝所有非 paper 模式。
- 前端按钮 disable 只是体验层；后端审批绑定、到期、执行前数据/risk recheck、idempotency 和 Kill Switch 才是安全真值。

## 输入、输出与宿主隔离

- MCP tool input schema 使用 `additionalProperties: false`、枚举和必填 `request_id`；后端 API 返回稳定 `code/error/message`。
- UI 用 DOM `textContent`/节点构造渲染不可信文本，不使用 `innerHTML`，不使用 `dangerouslySetInnerHTML`，不加载 CDN 或外部脚本。
- UI resource 使用自包含 CSP：self-only script/style/image/connect，`frame-ancestors *` 允许不同 MCP 宿主嵌入；真正的 MCP 宿主仍负责 sandbox iframe、宿主 CSP 和 postMessage 能力边界。
- bridge 只接受当前 window/parent message source，使用 JSON-RPC；没有访问 cookie、host DOM、host localStorage 或 session store 的代码。
- `window.openai` 仅 feature-detect fallback，不按 ChatGPT/Claude/其他产品名分支。

## 数据与日志

- 不使用 localStorage 保存账户、token、计划或审批；不在 console 打印敏感数据。
- 账户缺失显示 `N/A`，不填充虚构余额；金额从 Decimal 字符串格式化，客户端时间只负责展示，过期由后端时钟判断。
- 审计事件由后端脱敏摘要生成，SQLite 与 JSONL 对同一 event_id 只镜像一次；UI 只展示结构化事件和稳定 reason code。
- 本地 harness 的 blocked/partial/error/expired/conflict 场景可以合成展示 payload，只用于 UI 测试，不能连接真实 broker，也不能被当作真实成交证据。

## 可访问性与视觉安全

永久 PAPER TRADING badge、文本状态、风险 reason code、键盘 focus、文本按钮、响应式表格、图表文字摘要、light/dark、高对比度边框和 reduced-motion 是默认 UI 约束。风险状态不只依赖颜色；确认对话框明确区分批准和执行。
