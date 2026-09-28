# Venus 多人协作 · 执行计划

> 实施状态（2026-09-27）：M0/M1 身份、审计和多人审批已落地；团队任务 owner/visibility/project_id、私人任务权限过滤、独立 worktree 与变更单审阅/合并/冲突刷新/回退已落地。单 Hub 单团队的 Tailscale Serve 入队、一次性邀请、管理员批准、独立设备凭证和撤销也已实现。M2 的任务插话及 M3 的通用共享资产发布仍未实现。团队 Hub 部署与成员入队见第 8 节。

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
│ V1 客户端 ──── 独立设备凭证 │ V1 客户端 ──── 独立设备凭证
└────────┬───────────────────┘────────┬──────────────────
         │        Tailscale 虚拟局域网 │
         ▼                            ▼
   ┌──────────────────────────────────────────┐
   │ 团队 Hub（NAS / 常开小主机 / VPS）        │
   │ llm_server --isolated --team-serve        │
   │ 127.0.0.1:8001 ← Tailscale Serve HTTPS   │
   │ · agent_jobs（共享任务台）                │
   │ · confirm（双人复核审批）                 │
   │ · skills/ RAG/ agents/（共享知识）        │
   │ · team.json + 设备 token hash + audit     │
   │ telegram_bot（移动审批通道）              │
   └──────────────────────────────────────────┘
```

复用清单：`--isolated` 沙箱、`agent_jobs` + `/jobs/{id}/events` SSE、confirm 表、会话 `expected_version` 乐观锁、`secure_store`(Windows DPAPI)、Tailscale Serve 身份头——在既有能力上扩展。远程团队 Hub 只接收本机 loopback 和配置的 Serve 代理请求。

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
- 历史 M0 接口：`GET /api/v1/team`、`POST /api/v1/team/users`。单 Hub 入队现行流程已替代直接建员：新流程启用后该建员接口返回 410，见第 8 节；
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

> 当前：共享任务字段、列表/详情/SSE/取消权限和独立工作目录已实现；`scripts/start_team_hub.ps1` 与 `scripts/check_team_hub.ps1` 已提供 Tailscale Serve 启动和状态核验。Windows 服务/计划任务注册及 `/interject` 仍未实现；Hub 电脑重启后需重新运行启动脚本。

> **执行位置（v0.1 约定）**：团队 job 默认在 **Hub 的 `--isolated` 沙箱**内执行（共享工作区 + 文件类工具），**不会**调用成员本机 daemon 的屏幕/键鼠。若未来需要「在 A 的电脑上跑工具」，属于 Remote Worker 扩展，不在 M2 范围。

**当前部署**：`scripts/start_team_hub.ps1` 检查 Tailscale 主机名，启动只绑定 loopback 的 `--isolated --team-serve` 后端并设置 HTTPS Serve；`scripts/check_team_hub.ps1` 通过实际 HTTPS 输出团队名与 `team_id`。不向成员发通用 token；成员通过第 8 节的一次性邀请与审核获得设备凭证。

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

> 当前：技能、RAG 与 Agent 仍按 Hub 现有目录读取；“发布到团队”资产 API/UI 尚未实现。本次交付的 Git 项目变更协作不等于通用资产发布。

- Hub 的 `skills/`、`RAG/`、`agents/` 即团队资产（已是文件夹形态，零改造）；
- V1 技能/插件页加「发布到团队」= 复制到 Hub（走新 `POST /api/v1/team/assets`，admin 可回收）；
- 记忆系统不动——保持个人。

**验收**：A 写的 SKILL.md 发布后，B 的 Agent 目录里出现该技能。

### M4 · 协作会话（Phase 2，可砍）

> **状态**：Phase 2 可选；M0–M3 稳定后再立项。Telegram 群优先验证需求，再决定是否做 V1 内嵌版。

## 4. 安全设计（红线）

1. **身份与凭证分开**：成员由服务端记录的 Tailscale 登录名和角色识别；每台设备各有独立高熵凭证，Hub 只存 hash，客户端按 Hub origin/team_id 存入 `secure_store`（Windows DPAPI）；
2. **一票否决 + fail-closed**：审批超时/回传失败一律按拒绝（现有语义保持）；
3. **组网边界**：新入队 Hub 必须使用 `--isolated --team-serve`、仅监听 `127.0.0.1`，并经 Tailscale Serve 提供精确配置的 `*.ts.net` HTTPS 地址；只在 loopback 代理和精确 Host 同时满足时才信任 `Tailscale-User-Login`。不通过 Funnel 或直连 tailnet/LAN 暴露后端；
4. **审计不可抵赖**：审批、取消、发布、成员增删全量落 audit.jsonl，V1 做只读审计页（沿用 diagnostics 页版式）；
5. **隔离不破**：Hub 永远 `--isolated` 启动，脚本硬编码，无屏幕工具。

## 5. 改动量一览

| 模块 | 改动 |
|---|---|
| llm_server.py | 鉴权中间件 +users、confirm 表多票、/team* 端点、jobs 字段，~400 行 |
| agent_jobs.py | owner/members/visibility + scope 过滤，~60 行 |
| telegram_bot.py | 审批卡多人路由，~80 行 |
| venuschat_v1 | 身份引导、审批徽章、团队面板分组、设置页团队管理，~350 行 |
| scripts/ | start_team_hub.ps1 + check_team_hub.ps1 + 入队指南 |
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

## 8. 已交付：团队项目版本协作

### Hub 安全启动与单团队边界

第一阶段是**一个 Hub 对应一个团队**，最多 10 名活跃成员；每名成员可批准多台独立设备。Hub 需要安装 Git、Venus Python 依赖和已登录的 Tailscale。Tailscale 管理控制台需启用 HTTPS 证书，Hub 和成员设备都加入同一 tailnet。被标记的设备没有 `Tailscale-User-Login` 身份头，不能发起或领取成员申请。Serve 仅在 tailnet 内提供 HTTPS；不要配置 Funnel。

在 Hub 仓库根目录以管理员权限运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_team_hub.ps1
```

脚本确认 Hub 的 Tailscale MagicDNS 域名，只启动 `--isolated --team-serve` 后端并绑定 `127.0.0.1:8001`，再配置后台 Tailscale Serve 根路径转发到该 loopback 服务。已有其他 Serve 配置时脚本会停止并要求先人工检查，不会覆盖；成功后会打印 `https://<设备名>.<tailnet>.ts.net` 和 `tailscale serve status`。完成初始化后运行 `powershell -ExecutionPolicy Bypass -File scripts/check_team_hub.ps1`，它会通过实际 HTTPS 地址核对服务状态并显示团队名和 `team_id`。Windows 重启后需再次运行 `start_team_hub.ps1` 启动 Hub 后端；Serve 配置使用后台模式会自动恢复。停用 Venus 根路径可运行 `tailscale serve --https=443 off`。不允许将 `llm_server` 直接绑定局域网或 tailnet 地址。Serve 注入登录身份头并清理同名外部请求头；后端仍只通过 loopback 接收 Serve 流量。参见 [Tailscale Serve 身份头与 loopback 建议](https://tailscale.com/docs/features/tailscale-serve) 和 [Serve CLI 状态、HTTPS 与后台模式](https://tailscale.com/docs/reference/tailscale-cli/serve)。

首次启动时，Hub 的一次性初始化凭证由 Venus `secure_store` 保存在 Hub 本机用户的系统安全存储中，不写配置文件或日志，也不交给普通成员。打开 Hub 本机 VenusChat → 设置 → 团队与成员，填写 HTTPS Hub 地址并点“核对 Hub 地址”，确认团队尚未初始化；再填写团队名称、首位管理员实际 Tailscale 登录名和显示名，点“在本机初始化团队”。初始化请求只发往同一台 Hub 的 loopback，并校验本机后端的 `serve_host` 与确认的 HTTPS Hub 主机名一致。初始化创建持久 `team_id`、第一个管理员和这台设备的凭证。UI 随即按 Hub 地址把凭证写入 DPAPI，并通过 Serve 再验证身份。

旧 `users.json` 用户会保留历史记录并迁移为 `legacy_untrusted`；原 token 不再能访问新 Serve Hub。之后可按实际 Tailscale 登录名重新邀请这些成员，不会删除既有项目、Job 或审计历史，也不会静默提高信任级别。旧 `/api/v1/team/users` 直发 token 接口在新流程启用后返回 410。`.venus/team.json` 保存唯一团队身份，`.venus/team_access.json` 保存邀请、申请、设备与 token hash；邀请密钥、申请领取密钥和设备 token 都只存哈希。

### 管理员邀请与成员入队

1. 管理员须先在自己的 VenusChat“团队与成员”完成初始化并显示“已加入 · 管理员”。在“受邀 Tailscale 登录名”输入受邀者实际登录名（如 `person@example.com`），保持默认角色 `member`，点“创建邀请”。邀请码有效 24 小时且最多用一次。弹窗只显示一次 Hub HTTPS 地址、团队名、`team_id`、受邀登录名和邀请码。请通过已有可信渠道转交，不要把邀请码粘贴到团队仓库、任务、日志或 URL 中。可在该窗口关闭前复制邀请码。管理员可在邀请列表撤销仍有效的码。
2. 受邀者在自己的电脑登录 Tailscale 并打开 VenusChat → 设置 → 团队与成员；输入管理员提供的 HTTPS Hub 地址，点“核对 Hub 地址”，确认团队名称与 `team_id` 后填写邀请码、显示名、设备名并提交。客户端用不带旧 API Key 或团队凭证的 guest 请求，在用户确认的 HTTPS origin 将邀请码放在 JSON 请求体中。后端要求 Serve 提供的登录名与邀请指定登录名完全匹配；匿名、tagged 设备、身份不符、过期/撤销/重复码均被拒。错误请求、重定向或非 loopback HTTP 不会发送本机旧凭证。
3. 申请会显示编号和“申请待批准”。管理员在“刷新待审申请”查看预期登录名、实际 Serve 登录名、设备名与申请编号，自己核对后批准或拒绝。批准只创建待领取设备，不向管理员返回成员 token。申请人刷新状态，点“领取已批准的设备凭证并连接”；需同时具备申请时保存在本机 DPAPI 的一次性临时密钥和同一 Tailscale 登录身份。领取成功后临时密钥立即失效，设备 token 明文只在该领取响应一次出现，UI 先保存到该 Hub/team 的安全存储，再调用“我的团队身份”校验。
4. 登录相同、已存在的成员申请新设备时，审核通过会把设备加到同一 user_id，不会另建同名成员。设备 token 按 Hub origin 与 team_id 隔离；切换 Hub 时不会把旧个人 API Key 或别的团队 token 发过去。团队设备 token 是可撤销的访问凭证，不是硬件级设备证明或真人实名认证。

成员连接后可查看团队项目和 team scope Job；私人 Job、个人项目、个人记忆和本机文件沿用原隔离规则。团队成员不读完整审计，只能读管理员可读审计中的与本人相关条目。管理员可在成员/设备列表撤销单台设备，token 下次请求立即 401；可停用成员，使其所有设备失效并从新审批票数中移除。成员可点“退出此设备”撤销本机凭证。遗失设备时，管理员从另一台有效管理员设备撤销丢失设备，再为相同 Tailscale 登录名创建新邀请；如果成员整体被停用，则应重新邀请并由管理员批准。管理员不能停用最后一位管理员或撤销其最后一台可用设备。

写操作按 actor 与对象进入 `audit.jsonl`，并对关联 Job 追加事件；未领取的已批准设备在成员停用时也会失效。管理员拥有完整审计读取权。启用团队入队的 Hub 必须始终使用 `--team-serve` 启动，不能切回通用 token 或直连模式。

主要入队 API：`GET /api/v1/team/public`；本机一次性 `POST /api/v1/team/bootstrap`；管理员 `POST/GET /api/v1/team/invites`、`DELETE /api/v1/team/invites/{invite_id}`，`GET /api/v1/team/applications`、`POST /api/v1/team/applications/{application_id}/review`；匿名成员身份申请 `POST /api/v1/team/join/preview`、`POST /api/v1/team/join-requests`、本人 `GET /api/v1/team/join-requests/{application_id}`、一次性 `POST /api/v1/team/join-requests/{application_id}/claim`；成员 `GET /api/v1/team/me`、`GET /api/v1/team/me/devices`；管理员 `GET /api/v1/team/members`、`POST /api/v1/team/members/{user_id}/deactivate`、`POST /api/v1/team/devices/{device_id}/revoke`。错误语义使用 401（缺身份/凭证）、403（身份或角色不允许）、404（对象不存在/邀请码无效）、409（重复或状态冲突）、410（过期/撤销/已领取）、429（加入限流）。

### 版本协作的审批策略

多人审批需要在 Hub 的 `.venus/collab_config.json` 设置 `team_mode: true`，且至少有两名活跃成员。团队变更审阅使用 `default_required`，可用 `tool_required.team_change` 单独指定。例如：

```json
{
  "team_mode": true,
  "default_required": 1,
  "destructive_required": 2,
  "tool_required": {"delete_file": 2, "git_commit": 2, "team_change": 2}
}
```

### 初始化项目与提交变更

1. Hub 管理员先在 Hub 配置的工作区准备明确要共享的文件，例如 `team-demo/README.md`；初始化接口只复制 `shared_paths` 指定的路径，不会递归导入整个工作区。
2. 成员在 VenusChat 新建团队项目，选中该项目后点「团队协作」，填写 `team-demo` 或 `team-demo/README.md` 初始化版本库。目录初始化只导入安全文件类型，排除 `.env`、`.venus`、凭据、记忆、会话、审计、虚拟环境和临时目录。
3. 成员选中团队项目，用 `!任务描述` 派发任务。团队项目的 `/dispatch` 会始终创建异步 Job；直接 `chat/stream` Agent 请求返回 409，避免绕过隔离。每个 Job 从项目当前 HEAD 建立 `venus/<job_id>` 分支和独立 worktree。Agent 的相对路径文件工具以该 worktree 为根；项目共享范围之外的文件不会被版本提交接口接受。
4. 任务完成后，发起人在任务卡片点「提交变更」，点击「读取任务改动」，填写目的并确认每行一个路径；单次变更最多 100 个文件。提交会清除此前暂存区状态，然后只 stage 这次列出的路径。变更单保存作者、基础 SHA、当前 SHA、文件列表和任务 ID。
5. 团队成员在同一项目的「团队协作」窗口选择变更，查看目的、关联任务、文件差异、作者、SHA 和审阅历史，按当前 SHA 批准或拒绝。审批人数在任务创建时按绑定项目的 `default_required`（可用 `tool_required.team_change` 覆盖）固定；作者不能投自己的审批票，重复投票不会重复计数，拒绝会使本轮已有批准失效。界面显示有效批准票数；作者自审返回 403。
6. 已批准变更可以合并。合并只允许目标分支仍停在基础 SHA 时快进；成功的审阅、拒绝、合并、回退和任务状态变化会进入 `audit.jsonl` 或关联 Job 事件。

项目级 API 使用 `/api/v1/projects/{project_id}/versions` 和 `/api/v1/projects/{project_id}/changes/...`；任务提交、改动清单使用 `/api/v1/jobs/{job_id}/changes/commit` 与 `/api/v1/jobs/{job_id}/workspace`。`GET /api/v1/jobs?scope=mine|team|all` 返回当前用户有权查看的任务。私人 Job 的列表、详情、事件 SSE、确认待办和取消接口都执行权限检查。

### 冲突与回退

如果目标分支已前进，合并返回 409，并把变更标成「版本过期」；不会自动合并未经新一轮审阅的内容。作者点击「刷新基线」会显式 rebase 到最新 main：无文件冲突时生成新 SHA、使旧批准失效并重新进入待审阅；有文件冲突时 Git rebase 会中止，目标分支不变并返回 409。冲突无法通过基线刷新解决时，在最新版本上新建团队任务，重做需要的修改并提交新变更；旧变更和审阅记录保留供追溯。

已合并的变更可在协作窗口点「新建回退变更」。系统会在新分支上生成反向提交并创建新的待审阅变更单；原历史不删除，回退也需要另一名成员批准后再合并。对同一变更重复调用回退接口会返回同一回退变更。

如果反向提交与后来对同一文件的改动冲突，接口返回 409，并保留带冲突说明的任务/变更记录和审计项，不会重复创建回退。此时从最新版本创建新团队任务，按当前文件内容手工重做所需反向修改，再按普通流程审阅合并。

### 数据边界与手工验收

团队 Git 数据位于 Hub 的 `.venus/team_projects/<project_id>/`：`repo/` 为项目仓库，`worktrees/` 为每个任务的独立工作目录，`changes/` 保存变更单。项目元数据、Job、任务事件、审批和审计仍由 Venus 后端保存。团队 Job 不关联成员私人会话，也不读取或写入个人记忆/待办；任务结果通过团队任务面板查看。Agent 只看当前团队项目和已共享范围内的 `skills/*/SKILL.md`；Hub 本机技能、子 Agent 定义和个人动态技能不注入团队任务。个人任务继续使用原工作区和记忆流程。团队仓库只收录显式共享范围内、通过路径和凭据检查且由提交者明确列出的文件；屏幕数据、token、个人记忆、私人文件、会话、审计日志和后端元数据不进入 Git。

**两人演示：**

1. A 的电脑运行 `scripts/start_team_hub.ps1`，在 A 的 VenusChat 初始化团队；邀请 B 的实际 Tailscale 登录名。通过可信渠道交给 B Hub HTTPS 地址和一次性邀请码。
2. B 的电脑登录同一 tailnet，在 VenusChat 核对 HTTPS 地址/team_id、提交申请；A 查看实际 Serve 登录名与预期登录名后批准；B 领取并保存自己的设备凭证。A、B 都能访问团队项目和团队 Job，不共享私人任务或个人记忆。
3. A、B 分别选择同一个团队项目，在派发任务前各创建一个 Job；两个 Job 可基于同一 SHA，但有独立分支与 worktree。A 完成任务并提交明确路径的变更，B 在“团队协作”查看 diff、SHA 后批准，A 刷新并合并。
4. B 提交基于旧 SHA 的另一变更并请 A 批准。目标分支已前进时 B 合并应收到 409；B 点“刷新基线”，旧批准失效；A 检查新的 SHA/diff 并重新批准后，B 再合并。
5. 在已合并的变更上点“新建回退变更”，由另一名成员审阅并合并。项目 main 出现新的反向提交，原变更仍可追溯。随后 A 可在成员/设备面板撤销 B 的某台设备，B 的另一台已批准设备仍可访问。

**已自动验证：** `python tests/collab_contract_test.py`（身份/审计/M0-M1：23 项通过）、`python tests/team_versions_contract_test.py`（临时 Hub 数据目录：71 项通过，覆盖独立 worktree、Git 环境隔离与输出/路径数量上限、路径/凭据过滤、Agent 项目/记忆/技能边界、多成员审阅、409/rebase、追加提交失效、回退幂等、回退冲突审计、私人任务隔离），以及 `python tests/venuschat_v1/frontend_contract_test.py`（通过）。相关后端/GUI Python 文件通过 `py_compile`。GUI 需要在有显示器的 Windows VenusChat 中手工走完上面的两人流程；本次没有连接真实 Hub 或真实用户数据。
