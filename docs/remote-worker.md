# Venus Remote Worker（首版）

Remote Worker 让已加入项目的 Venus 终端主动连接 Hub，为指定项目执行受限文件操作。项目成员身份和 Worker 本机授权是两件独立的事：加入项目不会启动 Worker，也不会开放终端屏幕或命令执行。

## 支持范围

| 工具 | 参数 | 本机行为 | 项目审批 |
| --- | --- | --- | --- |
| `workspace.list` | `path` 可选 | 列举授权工作目录下至多 200 项 | 项目策略要求的非发起人票数 |
| `workspace.read` | `path` | 读取授权目录内至多 256 KiB 的 UTF-8 文本 | 项目策略要求的非发起人票数 |
| `workspace.write` | `path`、`content` | 仅新建文件，不覆盖已有文件 | 至少两名不同的非发起人批准，且宿主机本机逐次确认 |

命令、屏幕、键盘和鼠标工具返回不支持。Worker 不监听入站端口；它从已登记的 Hub HTTPS 地址出站轮询。设备凭证仍存于 Venus 现有安全存储中，授权文件只记录项目、目录、工具和期限。所有请求带 Hub 用户与设备身份，Hub 再核对项目成员关系、当前终端授权、目标终端、Job、调用参数摘要、有效票数和租约。

## 桌面操作流程

1. 在 VenusChat 的 **设置 → 项目中心** 选择已认领项目。负责人使用“项目成员与终端授权”完成终端邀请，终端所有者主动接受。
2. 终端所有者点击 **本机 Worker 授权**，填写一个现有工作目录、授权工具与 1–720 小时期限。核对 Hub、项目、目录和工具后点击“明确授权并启动”。窗口保持打开时执行出站轮询；关闭窗口会停止此次轮询。
3. 项目成员点击 **项目 Worker 任务**，选择目标终端、文件工具和相对路径，提交任务。Hub 创建一个团队 Job 和绑定的 Worker 调用，生成独立 `call_id`。
4. 合格成员在同一界面刷新调用列表，阅读完整参数和内容，再分别批准或拒绝。发起人不能为自己投票；每位成员只计一票。`workspace.write` 至少需要两名其他成员。因此只有两人的项目可读可列目录，但无法凑足写入票数。
5. 票数满足后，目标终端领取一次性租约。写入前，本机再次显示来源 Hub、项目、Job、调用 ID、路径、内容大小、哈希和内容预览，由终端所有者确认。拒绝会向 Hub 回报失败。终端本地会在执行前再次检查停止状态、授权期限、项目/设备/调用绑定及参数摘要。
6. 成员可在 Worker 调用列表查看 `awaiting_approval`、`approved`、`leased`、`completed`、`failed`、`rejected`、`expired` 或 `revoked` 状态。宿主机所有者可点击 **紧急停止**，或撤销此项目的本机授权。

本机授权一旦被撤销，正在轮询的 Worker 在下一次领取或执行前也会核对授权记录；替换旧授权不会让旧轮询继续工作。项目审批人降为只读或投票终端撤权后，原票不再计数；即使之后恢复角色或终端授权，旧票也不会自动复活，需要重新审批。拒绝、过期和撤销会使专用 Worker 任务结束，列表读取可修复跨存储中断后的任务状态。

同一能力也有交互式 CLI，适合终端或无图形界面的私人机器：

```sh
python src/remote_worker.py authorize \
  --origin https://hub.example.ts.net \
  --team-id team_example --project-id proj_example \
  --workspace /absolute/path/to/workspace \
  --tools workspace.list,workspace.read,workspace.write \
  --expires-seconds 28800
python src/remote_worker.py run
python src/remote_worker.py status
python src/remote_worker.py stop
python src/remote_worker.py revoke --project-id proj_example
```

授权 CLI 会要求在本机交互终端输入确认短语；`run` 必须保持在可交互终端里，才能逐次确认写入。不要把设备凭证放进命令行、项目任务正文或聊天消息。CLI 和桌面窗口使用相同的本机授权记录及紧急停止标记。

## 关键 API

以下路由均以 `/api/v1/worker` 为前缀，并由 Hub 的 Team Serve 设备认证保护。

| 方法与路径 | 用途 |
| --- | --- |
| `POST /tasks` | 一次性创建项目 Worker Job 与首个调用；提交时指定 `project_id`、`target_device_id`、`tool`、`args`、可选 `title`/`ttl_seconds` |
| `POST /calls` | 给仍在执行的现有团队 Job 添加调用，须带 `job_id`、`tool_call_id` 等绑定字段 |
| `GET /calls?project_id=...` | 列出该项目调用、参数、投票和状态；跨项目拒绝 |
| `GET /calls/{call_id}` | 获取单个调用详情与有效票数 |
| `POST /calls/{call_id}/votes` | `decision=approve/reject`，每人一次，非发起人 |
| `POST /poll` | 目标终端按项目领取已批准调用的一次性租约 |
| `POST /calls/{call_id}/preflight` | 凭租约重新核对任务、项目、设备、参数摘要和有效票数 |
| `POST /calls/{call_id}/complete` | 目标终端凭租约提交结果或本机拒绝/错误 |

一个调用由 `project_id`、`job_id`、目标 `device_id`、`tool_call_id`、工具、参数 SHA-256、到期时间、调用者用户与设备绑定。服务端只保存租约摘要。重复提交相同 `tool_call_id` 返回冲突；租约只可领取一次，超时后不自动重派发有副作用的操作。加入项目的终端只能访问已授权的项目调用；终端撤权、成员移除、票数不足、Job 取消或失败时拒绝继续执行。普通团队 Job 在执行期间提交的调用可以在 Job 正常完成后继续按自身 TTL 处理；新建 Worker 任务则由 `POST /tasks` 创建，避免需要抢在 Agent Job 完成前手动追加调用。

## 本机文件边界

相对路径不能有绝对路径、盘符、ADS、`..`、符号链接或目录联接；敏感文件名和密钥后缀被拒绝。写入目标的父目录必须已存在，写入采用独占新建，不覆盖已有文件。读取只返回 UTF-8 文本，结果和内容均有大小上限。本机文件不会自动提交到项目 Git，若要分享修改，仍需按项目变更单审阅。

本机授权防的是来自 Hub 的越权请求，不是同一操作系统账号下的恶意本地进程。授权目录应使用专门的项目工作目录，不要把个人主目录、密钥目录或系统目录作为 Worker 工作目录。

写入后的网络中断可能让本机已经创建文件、Hub 却未收到完成回执；租约不会自动重派发，操作者需要核对本机文件和调用记录后再决定是否另开任务。本机路径检查与文件打开之间仍依赖同一操作系统账号的可信环境，不能抵御该账号下恶意进程对目录的并发替换。

## 当前验收范围与限制

`tests/project_access_contract_test.py` 使用临时 Hub 数据和假 Serve Host 覆盖创建、邀请、两项目隔离、两票 Worker 调用、领取、预检、完成与撤权；`tests/remote_worker_contract_test.py` 覆盖本机路径、绑定、写入确认、只新建和停止逻辑。尚未在真实 Linux Hub、Tailscale Serve 和两台 Windows 终端之间进行联调，也未实际验证 Tk 窗口中的本机确认交互。屏幕、键鼠和命令执行仍未实现。部署前应按 [Linux 部署指南](multi-project-hub-deployment.md) 备份、迁移并进行双终端联调。
