# Venus 多人协作 · 执行计划

> 状态：M0/M1 后端已落地（`team_collab.py` + `tests/collab_contract_test.py`）｜ 版本：v0.1 ｜ 前置阅读：`docs/quant-integration.md`、`docs/venuschat-v1.md`

## 0. 一句话目标

让 2~10 人的小团队**安全地共用一个「团队大脑」**（共享任务、审批、知识），同时保证**每个人的「手」**（屏幕/键盘/私人文件/私人记忆）永不出本机。

## 1. 边界（非目标）

| 永不共享 | 明确不做 |
|---|---|
| 个人屏幕/键鼠控制（daemon 保持 loopback） | 账号体系 / OAuth / 密码登录 |
| 个人文件工作区 | 公网暴露端口（走 Tailscale 组网） |
| 个人记忆 L1/L2 | 实时协同编辑同一文件 |
| 私人会话（默认 private） | 复杂 RBAC（两档角色足够：admin/member） |

## 2. 架构总览

```
┌ 成员A笔记本                ┌ 成员B台式机
│ daemon :8000 (私有·loopback)│ daemon :8000 (私有·loopback)
│ V1 客户端 ──── token_A      │ V1 客户端 ──── token_B
└────────┬───────────────────┘────────┬──────────────────
         │        Tailscale 虚拟局域网 │
         ▼                            ▼
   ┌──────────────────────────────────────────┐
   │ 团队 Hub（NAS / 常开小主机 / VPS）        │
   │ llm_server --isolated --port 8001        │
   │ · agent_jobs（共享任务台）                │
   │ · confirm（双人复核审批）                 │
   │ · skills/ RAG/ agents/（共享知识）        │
   │ · users.json + audit.jsonl（身份与审计）  │
   │ telegram_bot（移动审批通道）              │
   └──────────────────────────────────────────┘
```

复用清单：`--isolated` 模式、`api_token` 鉴权中间件、`agent_jobs` + `/jobs/{id}/events` SSE、confirm 表、会话 `expected_version` 乐观锁、`secure_store`(DPAPI)、telegram 白名单——**全部现成，只扩不换**。

## 3. 里程碑

### M0 · 身份层（半天）⚠️ 所有后续工作的前提

**做什么**：把「单 token」升级为「每人一 token 一身份」。

```jsonc
// Hub: .venus/users.json
{
  "users": [
    {"id": "u_lyt",  "name": "梁神",  "token_hash": "sha256:…", "role": "admin",  "color": "#C9573D"},
    {"id": "u_wang", "name": "小王",  "token_hash": "sha256:…", "role": "member", "color": "#2F8A5B"}
  ]
}
```

- 鉴权中间件扩展：`X-User-Token` → sha256 比对 users.json → `request.state.user`；
- 旧 `api_token` 保留兼容 = owner 身份（个人机器零迁移）；
- 新端点：`GET /api/v1/team`（成员+角色）、`POST /api/v1/team/users`（admin 建员/轮换 token，token 明文只在建员时返回一次）；
- 审计：`audit.jsonl` 追加 `{ts, user, action, detail}`（所有写操作与审批必录）。

**验收**：两把 token 各发一条会话消息，`GET /api/v1/audit` 能看到两个身份的独立记录；错 token 401；旧 token 行为不变。

### M1 · 双人复核审批（1 天）★ 首发核心，产品气质所在

**做什么**：敏感操作从「一人允许」升级为「N 票通过才执行」。

```jsonc
// confirm 表条目扩展
{
  "request_id": "cf_xxx", "tool": "delete_file",
  "required": 2,                       // 该操作类别需要的票数（策略配置）
  "approvals": {"u_lyt": "yes"},       // 逐票记录，天然幂等
  "expires": 120
}
```

- 策略配置（Hub 侧 `collab_config.json`）：**须 `"team_mode": true` 且成员 ≥2** 才具备多人复核能力；**仅当活跃项目的 `is_team: true`** 时敏感操作才走 N 票，个人项目始终单人确认。
- `POST /api/v1/agent/respond` 带身份 → 记一票 → 广播 `approval_update` SSE（票数不足时任务继续等待而非拒绝）；
- 任一 member 投「拒绝」→ 立即终止（一票否决）；超时 → fail-closed 按拒绝；
- 新端点：`GET /api/v1/confirm/pending`（各客户端拉待办徽章）。

**UI**：
- V1 审批框头部加票数徽章 `1/2 已通过` + 已投票成员色点（PIL AA 圆片，沿用 MenuPopup 皮肤）；
- 顶栏「待我审批」红点计数（轮询 pending 端点，30s）；
- Telegram 审批卡同步推给**所有**票缺的 approver，按按钮即投票。

**验收**：三人团队删一个共享文件——A 发起，B 手机批准（显示 1/2），C 未批前 Agent 挂起等待；C 拒绝 → 操作终止且 audit 记录完整证据链；A 自己重复批准不改变票数。

### M2 · Hub 部署 + 共享任务台（1~2 天）

> **执行位置（v0.1 约定）**：团队 job 默认在 **Hub 的 `--isolated` 沙箱**内执行（共享工作区 + 文件类工具），**不会**调用成员本机 daemon 的屏幕/键鼠。若未来需要「在 A 的电脑上跑工具」，属于 Remote Worker 扩展，不在 M2 范围。

**部署脚本** `scripts/setup_team_hub.ps1`：venv → `llm_server --isolated` 注册为服务 → Tailscale 检查 → 生成首位 admin token → 输出各成员接入指引（`llm_base=https://hub.tailnet:8001` + 各自 token）。

**任务台扩展**（agent_jobs 字段级改动）：
```jsonc
{"id": "job_x", "owner": "u_wang", "members": ["u_lyt"],
 "visibility": "team",   // private | team，默认 private
 "title": "跑A股财报RAG入库"}
```

- `GET /api/v1/jobs?scope=team`：成员可见团队任务流；
- 任何成员可取消团队任务、可「插话」（新 `POST /jobs/{id}/interject`，文本注入任务下一轮上下文）；
- V1 执行面板加「团队 / 我的」两个分组，owner 色点标识。

**验收**：B 在 Hub 发起长任务，A 的 V1 实时看到同一进度条并可插话纠偏；A 取消后 B 收到通知。

### M3 · 共享知识（半天）

- Hub 的 `skills/`、`RAG/`、`agents/` 即团队资产（已是文件夹形态，零改造）；
- V1 技能/插件页加「发布到团队」= 复制到 Hub（走新 `POST /api/v1/team/assets`，admin 可回收）；
- 记忆系统不动——保持个人。

**验收**：A 写的 SKILL.md 发布后，B 的 Agent 目录里出现该技能。

### M4 · 协作会话（Phase 2，可砍）

> **状态**：Phase 2 可选；M0–M3 稳定后再立项。Telegram 群优先验证需求，再决定是否做 V1 内嵌版。

## 4. 安全设计（红线）

1. **token 即身份**：users.json 只存 hash；客户端 token 存 `secure_store`（DPAPI），不落明文配置；admin 可随时轮换单员 token；
2. **一票否决 + fail-closed**：审批超时/回传失败一律按拒绝（现有语义保持）；
3. **组网边界**：Hub 只监听 Tailscale 网卡；`host_guard` 白名单加 tailnet 段（100.64.0.0/10），公网面为 0；
4. **审计不可抵赖**：审批、取消、发布、成员增删全量落 audit.jsonl，V1 做只读审计页（沿用 diagnostics 页版式）；
5. **隔离不破**：Hub 永远 `--isolated` 启动，脚本硬编码，无屏幕工具。

## 5. 改动量一览

| 模块 | 改动 |
|---|---|
| llm_server.py | 鉴权中间件 +users、confirm 表多票、/team* 端点、jobs 字段，~400 行 |
| agent_jobs.py | owner/members/visibility + scope 过滤，~60 行 |
| telegram_bot.py | 审批卡多人路由，~80 行 |
| venuschat_v1 | 身份引导、审批徽章、团队面板分组、设置页团队管理，~350 行 |
| scripts/ | setup_team_hub.ps1 + 成员接入模板 |
| tests/ | collab 契约测试（身份/投票/幂等），沿用现有免界面风格 |

## 6. 排期建议

```
第1个周末   M0 + M1     → 立刻可用「双人复核」向团队证明价值
第2个周末   M2          → 团队任务台上线
第3个周末   M3          → 知识共享
（M4 视使用反馈再立项）
```

## 7. 待拍板（默认值已选好，不回复按默认走）

| # | 问题 | 默认 |
|---|---|---|
| 1 | 团队规模 | 按 ≤5 人设计，10+ 再说 |
| 2 | Hub 放哪 | 你的常开机 + Tailscale；没有则 10 元/月 VPS |
| 3 | 第二票在哪投 | Telegram 优先，V1 弹窗兜底，审批网页不做 |
| 4 | 一票否决要不要 | 要（安全默认） |
