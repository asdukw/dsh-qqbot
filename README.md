# DSH QQ Bot

> 一个把 DeepSeek Harness（DSH）接到 QQ 上的聊天机器人。NapCat 负责 QQ 协议，DSH 负责 agent runtime、会话记忆、文件工具和可插拔的画像插件。

项目还处于早期阶段。DSH 本身是 developer preview，当前固定 `0.2.1rc2`，升级可能带来不兼容变更。

## 特性

- **QQ 私聊聊天**，群聊默认只回复 @ 消息（`reply_group_at = false` 可关闭）
- **赛程**：赛程类回答由模型输出 ```schedule JSON，adapter 用 Pydantic 校验后渲染成固定格式（阶段/北京时间/对阵/BO），校验失败不会把半成品发出去；
- **语音回复**：模型在短回复末尾写 `[语音]`，按 provider（edge / StepFun / Fish Audio）合成并发送 QQ 语音；主动接话按 `[voice] probability` 概率转语音。中文音色清单见 [`docs/voices.md`](docs/voices.md)（`scripts/export_fish_voices.py` 可重新导出）；
- **梗图**：模型写 `[梗图: 文字]`，从表情库里挑一张当底图、加字幕后发出去；
- **表情库**：群里别人发的 QQ 表情会自动收藏（按内容去重，重编码副本用 dHash 合并），模型写 `[表情: 语气词]` 按描述挑一张发出；`/sticker` 管理，面板里可以删除，也可以同时屏蔽相似图片；
- **长期记忆**：`memory.db` 的 FTS5 trigram 索引会在回复前检索当前会话的相关历史，作为 `[相关历史]` 注入；
- **关系 / 好感度**：每个 QQ 有 0 到 100 的好感度、关系标签和印象，模型用 `[好感+2]` / `[印象: ...]` 这类隐藏标记更新；`/relation` 所有人可查，群聊只看公开印象；
- **订阅转发**：`/sub add <B站UID|空间链接|RSS>` 把 UP 动态或任意 RSS 转到当前群/私聊；B 站直连被机房 IP 风控，默认走可配置的 RSSHub 镜像；
- **戳一戳**：被戳会像被叫到一样回应（8 秒冷却）；群里别人被戳会作为一条上下文 `[戳了戳 某人]` 记下来；
- **定时任务**：管理员直接说「每天早上8点提醒我起床」即可创建，说「把喝水那个删了/暂停」也能管理（模型输出 `[定时: ...]` / `[定时列表|删除|暂停|恢复]`，adapter 解析 + Pydantic 校验）；任务绑定创建时的群/私聊，可显式选 here/private/group；支持 fixed/generate 两种文案（generate 到点按人设现写）；`/task list|delete|pause|resume|run`；
- **每日日报**：每天 24:00 给当天活跃的群发一份趣味总结，包括水王榜、首条/末条、热词、复读现场、今日金句和今日宜忌，再加一段按角色卡口吻写的锐评；私聊可在 `[daily]` 里开关；`/daily [YYYY-MM-DD]` 可手动预览/补发；
- **入群/好友审批**：NapCat 推送的好友请求、群邀请和入群申请会进入管理面板「审批」页；管理员点同意/拒绝后由 bot 通过 OneBot 执行，`approvals.db` 负责去重、状态和失败重试；
- **联网搜索**：DSH 官方的 `web_search` / `web_fetch` 工具；搜索 provider 用免密钥后端（与 OpenCode 自带 websearch 同类），不需要额外 API key；
- **角色卡**：SillyTavern Character Card V1 / V2 / V3，支持 `.json` 和 `.png`（读取 `ccv3` → `chara` → `iTXt`）；SQLite 角色库，`/card search/install/use` 热切换
- **DSH session**：每个 QQ 会话一份持久化 JSONL 历史，自动上下文压缩；`session-history` 插件让模型按需读取旧 generation
- **用户画像**：内置 `user-profile` 插件，SQLite 存储，提供 `profile_read` / `profile_write` / `profile_forget`
- **文件记忆**：模型可使用 `read` / `write` / `edit` / `glob` / `grep`，工作目录限定在 `/var/lib/qqbot/dsh-workspace`
- **无 shell**：DSH profile 中禁用了 bash / pwsh / PTY / jobs，只保留文件与搜索工具
- **OneBot 正向 WS**：带 token 鉴权、断线重连、消息去重、长回复自动拆分
- **systemd 常驻**：由 `qqbot.service` 托管，崩溃后自动拉起

## 架构

```
QQ 手机 / 桌面
     │
NapCat (Docker, OneBot 11 WS, 127.0.0.1:3001)
     │
qqbot adapter (Python, systemd)
     │  DeepSeek Harness Python SDK
DSH runtime (custom profile: qqbot)
     ├─ llm-pi-ai  →  OpenCode Zen / DeepSeek / OpenAI-compatible
     ├─ session + compaction（JSONL 持久化）
     ├─ read / write / edit / glob / grep
     └─ user-profile 插件（SQLite）
```

- **adapter 只做渠道层**：连接 NapCat、鉴权/重连、QQ 会话 → DSH `session_id`、私聊/群 @ 策略、长消息拆分。
- **DSH 做 agent 层**：系统提示词、历史、压缩、工具、画像插件。
- 不把 QQ 做成 DSH 插件，是因为 DSH 还在 preview；渠道层放在独立进程里，可以独立升级、重启和排障。

细节见 [`docs/architecture.md`](docs/architecture.md)。

## 快速开始（Ubuntu 24.04+ / Debian 13+，需要 Docker Compose v2）

### 0. 准备

- Linux 主机，1 核 2G 内存起步（内存不够时加 2G swap）
- Docker + Compose
- Python 3.12+
- 一个 QQ 小号（不要用主号）
- 一个 OpenAI-compatible 模型 API key（默认 OpenCode Zen）

```bash
sudo bash deploy/prepare-host.sh --swap
```

如果你在国内，建议给 Docker 配镜像加速：

```bash
sudo bash deploy/prepare-host.sh --mirror https://your.mirror.example
```

### 1. 启动 NapCat 并登录

```bash
sudo bash deploy/napcat/setup-napcat.sh
sudo napcat-qr            # 打印二维码；过期后执行 sudo napcat-qr --fresh
```

用手机 QQ 登录小号并扫码。NapCat 的 `6099`（WebUI）和 `3001`（OneBot WS）只绑定 `127.0.0.1`，**不要暴露到公网**。

### 2. 安装机器人

```bash
sudo bash deploy/install-bot.sh "$PWD"
```

脚本会：

- 创建 `qqbot` 系统用户；
- 安装 Python venv、`deepseek-harness-sdk==0.2.1rc2`、`websockets`、`httpx`；
- 创建 DSH 自定义 profile `qqbot` 和 `user-profile` 插件；
- 安装并启用 systemd 单元 `qqbot.service`；
- 如果 NapCat 已登录，自动把机器人 QQ 号写进 `config.toml`。

### 3. 配置模型 key

```bash
sudoedit /etc/qqbot/bot.env
```

`bot.env` 只放密钥和路径，填 key 即可：

```ini
OPENCODE_API_KEY=sk-...
ADMIN_UI_PASSWORD=换一个只有你知道的密码
```

说明：

- 管理面板的登录密码来自 `ADMIN_UI_PASSWORD`，不设置时面板会拒绝登录；`ADMIN_UI_SECRET` 可选，留空时用密码作为 cookie 的签名密钥。
- 用哪个模型、上下文多长、输出上限、推理强度，全部在[模型注册表](#模型注册表)里配置，不再读 `LLM_PROVIDER` / `DSH_MODEL` 等环境变量（旧变量会在启动日志里提示被忽略）。
- 供应商（provider）保存 `api` / `base_url` / `api_key`；每个模型只声明自己属于哪个供应商。API Key 直接在 Admin UI 里填写，保存在 `models.db`（文件权限 600）。
- 默认的 OpenCode 行用 `OPENCODE_API_KEY`。
- OpenCode Go 订阅使用 `https://opencode.ai/zen/go/v1`，请求必须带 `x-opencode-session`。注册表用的是 DSH 内置的 `opencode-go` provider，这个头由 DSH 按会话自动加；注册表只覆盖 `apiKeyEnv`、`baseURL` 和模型参数（context window / effort 等）。
- 想换 provider：在 Admin UI 的「模型」页先添加供应商、再进入供应商添加模型；也可以继续用 `--set-model`（首次带上 `base_url`）新增，再 `--use-model` 切过去。

> `deepseek-v4.1-flash-free` 只能在 OpenCode 客户端内使用；从机器人直接调用会返回 `403 FreeTierError`。机器人请使用付费的 `deepseek-v4.1-flash`，或换一个 provider。

### 4. 启动

```bash
sudo systemctl start qqbot
sudo journalctl -u qqbot -f
```

给小号发一条私聊消息，确认回复。

## 命令

管理员 QQ 号写在 `ADMIN_QQ_IDS`（逗号或空格分隔，默认读 `/etc/qqbot/bot.env`）。`/relation` 的查询、`/story` 的个人剧情操作和 `/choose` 对所有玩家开放（剧情开关与管理员专属操作见下文）。普通用户还可在**私聊**使用 `/card` 浏览、搜索、查看和切换自己的角色；群聊角色卡命令、角色库管理及其他命令（包括全局 `/help`）仍要求管理员身份。非管理员群聊角色卡命令和其他管理命令会被静默忽略，私聊角色库管理操作返回权限提示。

| 命令 | 作用 |
|---|---|
| `/relation [QQ]` | 查看 bot 对你的印象；`set` / `reset` 仅管理员，见[关系与好感度](#关系与好感度) |
| `/help` | 显示帮助 |
| `/model` / `/model <序号\|model_id\|provider/model>` | 列出模型 / 切换激活模型，立即热切换 |
| `/card ...` | 普通用户私聊可浏览与切换自己的角色；安装、删除及别名管理仅管理员，见[角色卡](#角色卡) |
| `/proactive on\|off\|status\|test` | 群聊主动接话的开关、状态和试触发 |
| `/sticker ...` | 表情库：列表、搜索、收藏、导入、打标、发送、禁用、删除（可同时屏蔽相似图），见[表情库](#表情库) |
| `/task ...` | 定时任务：列表、`add`、`delete`、`pause`、`resume`、`run`，见[定时任务](#定时任务) |
| `/sub ...` | 订阅：`add <B站UID\|空间链接\|RSS>`、`list`、`del`、`check` |
| `/daily [YYYY-MM-DD]` | 立即生成当前会话的每日日报（默认昨天，用于预览或补发） |
| `/reset`（或 `/new`） | 归档当前 DSH 会话，下一条消息开始新会话 |

命令不会写入对话历史，也不会送进模型。模型回复里的隐藏标记（`[语音]`、`[梗图: ...]`、`[表情: ...]`、`[定时: ...]`、`[好感+2]` 等）由 adapter 解析并剥离，不会出现在聊天里。

## Admin UI

面板是 `admin/` 下的 Vite + React + Tailwind 应用，组件按 shadcn/ui 的方式直接放在 `src/components/ui/`，主题为蓝白。页面按内容分组，左侧导航 + hash 路由，每个功能独立成页：

- **仪表盘**：今日消息 / Token、7 / 30 天趋势、订阅异常、最近消息、当前模型与角色卡；
- **对话**：聊天分析（今日发言排行、词云、会话分布）；词云用前端 SVG 螺旋布局，字号 = 覆盖消息数，不依赖额外图表库；
- **自动化**：审批、定时任务、订阅推送；
- **内容**：模型（供应商 → 模型两级管理，可新增、编辑、启停、切换当前模型）、角色卡、表情库（可删除并屏蔽相似图片）；
- **用量**：Token 用量，支持 7 / 30 / 90 天与按会话筛选；
- **系统**：数据库路径与大小、DSH 路径、面板配置。

Python 端只提供 JSON API 和静态文件：

- 本地构建：`cd admin && npm install && npm run build`；产物在 `admin/dist/`（已随仓库提交，便于纯 Python 部署）。
- 服务端：`bot/admin_ui.py` 默认从 `/opt/qqbot/admin/dist` 提供页面。读取接口：`/api/session`、`/api/login`、`/api/logout`、`/api/overview`、`/api/models`、`/api/cards`（点开单张卡时用 `/api/cards/<id>` 取完整 V3 payload）、`/api/tasks`、`/api/feeds`、`/api/stickers`、`/api/requests`、`/api/system`、`/api/usage`、`/api/fun`；`/api/dashboard` 保留为旧版聚合接口。模型页的写入接口（需登录、JSON、同源）：`POST /api/models/providers`、`/api/models/providers/delete`、`/api/models/items`、`/api/models/items/delete`、`/api/models/active`；审批页的写入接口是 `POST /api/requests/action`，表情库的删除接口是 `POST /api/stickers/delete`（同样需要登录、JSON 和同源）。登录仍用 `ADMIN_UI_PASSWORD` + HMAC cookie。
- 配置：`[admin] static_dir`；部署后 `systemctl restart qqbot-admin`。
- 模型页可写，其余页面只读；每 60 秒自动刷新，右上角「刷新」会立即重拉当前页。API Key 直接在供应商表单里填写，保存在 `models.db`（权限 600）且不会回显。

## Token 审计与今日趣味数据

- `usage.db` 记录每次模型调用的非缓存输入、缓存输入和输出：DSH 的每个 assistant step、compaction summary，以及图片描述、表情打标、任务解析/文案等 thin client 调用。
- Admin UI 的「Token 用量」支持 7/30/90 天和按群聊/好友筛选，展示缓存命中率、按用途、按 session 的排行。
- `memory.db` 的 `chat_events` 记录每条群聊/私聊入站消息；Admin UI 的「今日发言排行」「今日词云」按当天数据生成。词云先用 jieba 词性标注，只保留名词、动词、形容词、成语和英文，丢弃代词、连词、介词、数词、量词、语气词等；再排除成员昵称、停用词和纯语气词。`count` 是「提到该词的消息数」而不是原始词频，同一条消息里重复只算一次；长区间用 `ROW_NUMBER()` 均匀抽样（最多 2000 条），并默认过滤只出现一次的词。未安装 jieba 时回退到正则分词 + 停用词。
- 配置在 `[usage]`：`database`、`retention_days`、`event_retention_days`。

## 每日日报

每天 00:00（`[daily] hour/minute`，Asia/Shanghai）给「前一天有消息的会话」发一份日报；默认只发群，`private = true` 时私聊也发。

- **统计是确定性的**：消息数、参与人数、活跃度星级、发言排行（水王/亚军/季军）、首条与末条、热词（按覆盖消息数，jieba/正则）、复读现场（同一句话由至少两个人连续刷 ≥ `min_repeat` 次）、随机金句、bot 自己的回复数、按日期和会话固定种子的「今日宜/忌」。
- **锐评可选**：一次 thin client 调用，把统计和聊天节选交给当前模型，按角色卡口吻写 2-4 句；失败或超时就只发统计块，不影响日报本身。调用记录在 `usage.db`（purpose `daily_summary`）。
- **不污染会话**：日报走 OneBot 直接发送，不写入 DSH session / `messages` 历史，也不进入模型上下文。
- **去重与补发**：`daily.db` 记录每个 `(会话, 日期)` 的状态（`sent` / `skipped` / `failed`）；重启后在 `max_late_hours`（默认 6 小时）内会自动补发，已发送的不会重复。少于 `min_messages` 的会话标记为 skipped，不发日报。
- **配置**：`database`、`enabled`、`hour`、`minute`、`groups`（空 = 所有群）、`private`、`min_messages`、`max_late_hours`、`check_seconds`、`sample_chars`、`max_chars`、`max_attempts`、`min_repeat`、`prompt`（追加的风格提示）。
- **手动**：管理员在任意群/私聊发 `/daily` 生成昨天的日报，或 `/daily 2026-10-24` 指定日期。手动生成会忽略 `min_messages`。

## 入群/好友审批

NapCat 实现的是 OneBot 11，好友请求和群请求都会以 `post_type=request` 事件推给 adapter：

| 事件 | OneBot 字段 | 页面类型 |
|---|---|---|
| 好友请求 | `request_type=friend`，`user_id` / `comment` / `flag` | 好友请求 |
| 邀请 bot 入群 | `request_type=group`，`sub_type=invite`，`group_id` / `user_id` / `flag` | 群邀请 |
| 申请加入群 | `request_type=group`，`sub_type=add`，`group_id` / `user_id` / `comment` / `flag` | 入群申请 |

- adapter 解析后写入 `approvals.db`，按 `(kind, flag)` 去重；重复推送不会产生多条记录。
- 管理面板「审批」页列出待处理 / 全部请求，显示申请人、群、验证消息、状态和执行错误。
- 点「同意」/「拒绝」只是把决定写成 `queued`；bot 进程里的 worker 每 `[approvals] check_seconds`（默认 3 秒）通过现有 OneBot WS 调用 `set_friend_add_request` / `set_group_add_request`，成功后标记 `approved` / `rejected`，失败标记 `failed`（可重试）。这样 Admin UI 不需要额外的 NapCat 端口或 token。
- 状态：`pending` → `queued` → `executing` → `approved` / `rejected` / `failed`。bot 重启时会把中断的 `executing` 重新排队。
- 配置：`[approvals] enabled`、`database`、`check_seconds`；事件本身不需要改 NapCat 配置。

## 角色卡

角色卡统一入库到 SQLite `/var/lib/qqbot/characters.db`，内部一律存为 V3 结构（V1 平铺字段和 V2 的 `chara_card_v2` 都会自动升级）。普通用户在私聊中可浏览已有卡片并选择自己的角色；管理员另可管理共享角色库及群聊角色：

| 命令 | 作用 |
|---|---|
| `/card` | 列出角色卡，`*` 为全局默认；标题显示当前会话角色 |
| `/card search <关键词>` | 搜索名称、昵称、简介、性格、场景、标签、作者；空格分词按 AND 处理 |
| `/card install <url\|路径>` | 安装 JSON / PNG 卡，支持 `http(s)://`、`file://` 和服务器绝对路径 |
| `/card install`（附图片） | 把管理员发送的 PNG 卡图片直接安装 |
| `/card use <id\|名称>` | 仅切当前会话：预告 → 新角色 → 原卡开场白；其他群/私聊不变 |
| `/card show <id\|名称>` | 查看标签、别名、作者、简介和世界书条目数；来源与安装者仅管理员可见 |
| `/card delete <id\|名称>` | 从库中删除（默认、绑定或切换任务引用的卡不可删除） |
| `/card alias` | 查看当前卡的名称与别名 |
| `/card alias add <名称>` | 添加别名：群里直接叫它（至少 2 个字）也会被当成在找它 |
| `/card alias remove <名称>` | 删除别名 |

普通用户私聊可用 `/card`（或 `/cards`）、`list/ls`、`search/find`、`show/info`、`use/switch`、`help/?`，切换目标支持 ID、名称或已有别名。安装（`install/add`）、删除（`delete/rm/remove`）和别名管理（`alias/aliases/nick`，含查询）仍仅管理员可用。切卡固定作用于发送者本人的私聊，不修改全局默认，也不影响他人的私聊或任何群。所有已安装角色均可供私聊选择。

私聊选角示例：

```text
/card
/card use 2
/story start
```

剧情需管理员先开启；换卡不会删除原角色存档，切回后 `/story continue` 恢复原剧情。切卡任务执行时会再次校验操作者与目标会话，投递未知仍需管理员检查和人工恢复，不提供普通用户恢复入口。

### 管理员自然语言操作（QQ）

启用 DSH 并安装 `card-tools.mjs` 插件后，管理员可在私聊或正常被叫到的群聊中直接说：

- 「看看库里有哪些角色卡」「在库里找图书管理员角色」「介绍一下角色卡 2」
- 「去网上找一张温柔的图书管理员角色卡，下载到库里」
- 「下载第 2 张」「把刚才第二张装上」
- 「切换到角色卡 2」「切换到刚安装的角色」

`/card search` 仍只搜索本地库；在线搜索使用现有 DSH web provider，返回实际来源页和下载候选。**只搜索不会安装，安装不会自动切卡**；明确要求下载且只有一个候选时可直接安装，多候选请回复「下载第 N 张」。候选绑定同一管理员和同一 QQ 会话，10 分钟失效，新搜索替换旧编号；不将剧情裸数字当安装确认。删除、别名管理仍使用管理员 `/card` 命令。

支持公开 JSON/PNG 直链，以及静态 HTML 页面中的实际下载链接或直接嵌入的卡 JSON（包括能在公开页面找到下载链接的 Character Tavern 卡）。需要登录、依赖执行页面脚本或没有真实下载入口的来源会明确失败，不猜测站点 API/CDN 地址。安装前严格验证卡结构、大小与公网 HTTPS 目标，重复卡按指纹去重。无法自动识别某来源时仍可由管理员显式 `/card install <url>` 手动导入。

语义操作只对真实管理员消息开放，普通用户仍用私聊 `/card` 选角。权限不来自角色卡、网页或模型自报身份。首版每个管理员聊天轮启动一个独立 DSH runtime 传递短期授权 nonce，沿用同一 session 历史，但增加一些启动开销。DSH 关闭时提示改用 `/card`，不假报执行。工具切卡只返回「已受理」，当前文本回复所有分片获得发送回执、释放会话锁后才排队切换；失败或取消不切卡，已经入库的卡不会因此回滚。

本地验证：`python scripts/check_card_tools_runtime.py` 使用固定 SDK、临时库及本地假模型/搜索 fixture，不调用 QQ 或外部模型。

命令行等价操作：

```bash
M="sudo -u qqbot /opt/qqbot/bot/venv/bin/python /opt/qqbot/bot/main.py"

$M --list-cards
$M --search-cards 图书
$M --install-card https://example.com/card.png
$M --use-card 2 --conversation group:123456
$M --switch-status <任务ID>
$M --add-card-alias 肥鱼
$M --remove-card-alias 肥鱼
```

- 按内容指纹去重：同一张卡重复安装会返回已有记录，不会产生副本；
- 别名按卡保存（`cards.aliases`），和卡片自带的 `name` / `nickname` 一起参与「叫名字」判定；单字别名会被忽略；
- 下载上限 12 MB；只接受 PNG 或 JSON，PNG 依次查找 `ccv3`、`chara` 的 `tEXt` / `iTXt` 块；
- 卡里的 `description` / `personality` / `scenario` / `mes_example` / `system_prompt` 会渲染成 DSH 的系统提示词；
- 每个会话独立绑定卡片版本，角色和世界书经 agent-scoped prompt 注入，不再共享 `memory/lore.md`；
- 局部切卡不重启 DSH runtime；仅目标会话开启新 generation，并隔离旧人设的自动历史/recall。旧档案和用户画像不删除；
- 安装只入库，不广播；CLI 切换必须显式 `--conversation group:<id>` 或 `private:<id>`，可重复参数向指定会话 fanout；
- 同卡重复切换不重复开场。超时或发送中断记为投递未知，不自动重发；使用 `--switch-status` 检查回执，人工确认后才能继续（见运维文档）；
- **升级兼容**：库为空时自动导入 `[cards] bootstrap`（默认 `/etc/qqbot/character.json`）。未被绑定/切换任务引用的默认 bootstrap 卡仍可热加载；被引用的版本保持不变，需要安装新版本再显式切换。
- 仓库自带一张社区整理的示例卡 [`cards/dafeiyu.json`](cards/dafeiyu.json)（「大肥鱼」，DeepSeek 二创鲸鱼娘）；安装脚本会把它放到 `/opt/qqbot/cards/`，用 `--install-card` 或 `/card install` 入库。

## 表情库

群里别人发的 QQ 表情会自动收藏，模型也能按语境挑一张发出去（回复末尾写 `[表情: 语气词]`）。管理员命令：

| 命令 | 作用 |
|---|---|
| `/sticker` | 列出最近收藏的表情包（群里收到的表情会自动入库） |
| `/sticker search <关键词>` | 按描述/标签/来源搜索 |
| `/sticker add`（附图） | 把管理员发的图片收藏进表情库 |
| `/sticker import` | 导入 QQ 账号里的收藏表情 |
| `/sticker delete <id> [block]` / `ban` / `unban` | 删除或禁用/恢复；带 `block` 删除时会屏蔽相似图片 |
| `/sticker stats` | 数量与占用空间 |
| `/sticker tag <id> <描述> [标签,标签]` | 手动补描述/标签，发送时按语境挑 |
| `/sticker send <id>` | 手动发一张，验证发送链路 |

入库先按 SHA1 精确去重，再用 64-bit dHash 近似判重：重编码或轻微压缩的同图会复用已有记录。

删除表情时，如果选择「同时屏蔽相似图片」（命令里带 `block`），会把这张图的 64-bit dHash 写入 `sticker_blocks`，并删除库里所有 Hamming 距离 ≤ `[stickers] phash_distance` 的同类图片。以后群聊里再收到相似图片会直接拒绝入库。Admin UI 的表情详情页也有「删除表情」按钮，默认勾选该选项。

## 定时任务

管理员直接说「每天早上8点提醒我喝水」即可创建，模型输出 `[定时: ...]` 交给 adapter 解析；说「把喝水那个删了」也能管理。也可以用命令：

| 命令 | 作用 |
|---|---|
| `/task add <一句话>` | 语义化创建定时任务（仅管理员），如 `每天早上8点提醒大家喝水` |
| `/task` / `/task list` | 列出任务、下次执行时间和目标会话 |
| `/task delete\|pause\|resume\|run <id>` | 删除 / 暂停 / 恢复 / 立即执行一次 |

## 记忆

四层：

1. **会话记忆**：DSH session JSONL，位于 `/var/lib/qqbot/dsh-home/sessions`，私聊按人、群聊每群共用一份，自动压缩；个人剧情不使用群 DSH session。
2. **用户画像**：SQLite `/var/lib/qqbot/dsh-state/profile.db` 的 `user_facts`，按 `(user_id, group_id, key)` 存；私聊使用空 group_id，群事实按群隔离。每条事实带可见范围：私聊里得知的默认 `private`，只在私聊注入，群里看不到也改不了；群里得知的是 `public`。工具：`profile_read` / `profile_write` / `profile_forget`。
3. **群画像**：同库的 `group_facts`，按群号存，记群主题、氛围、常聊话题、梗和黑话、群规、约定。只在该群里注入。工具：`group_profile_read` / `group_profile_write` / `group_profile_forget`（私聊里不可用）。
4. **文件记忆**：`/var/lib/qqbot/dsh-workspace/memory/`；不再写共享 `lore.md`，人设按 session 注入；共享 `notes/` 不作个人剧情或私密关系后端。

会话 JSONL 不在文件工具的工作区内，所以另有一个只读的 `session-history` 插件，只在模型主动调用时读取，不会每轮注入：

- `history_sessions`：列出当前群/私聊的 session 文件（当前 + 旧 generation，含起止时间、轮数、大小）；
- `history_read`：读取某个 session 的 JSONL，返回用户消息和助手回复，支持关键词、`since` / `until`、条数限制；
- `chat_search`：检索 `memory.db.chat_events` 的入站消息，包含群里未 @ 机器人的普通消息（保留 90 天，不含助手回复）。

三个工具都只读，并且只允许访问当前群/私聊，不能跨会话读取。模型在用户问到「昨天/之前聊了什么」时会先查再答。

### 群聊上下文与活跃度

群里没 @ 机器人的消息会按群留在内存里最近 `group_context_messages`（默认 30）条；被 @ 时，这些消息和 @ 的那句一起交给模型，它就能接上话题。每条只送一次，不写盘，不跨群。

机器人会在这些情况下开口：

- **被 @**：直接回复；
- **被名字直呼**：消息里出现当前角色卡的 `name` / `nickname`（切卡后自动跟着变），或 `[bot.proactive] aliases` 里的额外名字，也算被叫到，直接回复；
- **引用回复**：引用了机器人最近发的消息（保留最近 200 条消息 id），也直接回复；
- **主动接话**：其余消息按 `[bot.proactive]` 判定。`probability`（默认 15%）逐条掷骰，命中后还要满足冷却 `cooldown_seconds`（默认 90s）、小时/天配额（12/60）、最少上下文条数、安静时段；`keywords` 命中时跳过概率直接尝试；
- **接话窗口**：机器人刚在群里说过话后 `followup_seconds`（默认 300s）内，概率乘以 `followup_multiplier`（默认 2）。

`/proactive status` 可以查看当前概率、冷却、配额和本群用量；`/proactive on|off` 临时开关。

### 身份如何传给 DSH

DSH SDK 的 prompt 接口只有 session id 和消息内容，没有元数据字段。所以 adapter 每轮调用前，把这个 session 的 `scope`（private/group）、QQ 号、群号、群名片写进 `profile.db` 的 `conversations` 表（`bot/conversations.py`），插件按 session id 查这张表拿身份，**不解析 session id**。查不到身份的会话，画像工具会直接报错。

## 关系与 Galgame 剧情

管理员在面板「剧情与关系」开启，或配置 `[story] enabled = true`。**剧情自动生成，不需要手写剧本**：首次切卡先生成并验证角色背景、章节、关键选择和结局骨架，再绑定角色并发送公共序幕。默认卡可直接 `/story start` 初始化。模型只负责文本，条件、效果、阶段和存档由程序控制。

- 群里共用公共背景，但每位用户的剧情、选项与关系独立；群内个人剧情是公开消息，不代表私密。不同群/私聊/角色相互隔离。
- 每幕显示编号选项。`/choose <菜单码> <编号>`，或引用菜单消息回复数字。群里普通裸数字不被剧情抢占，其他用户不能代选；私聊仅当前菜单接受数字。
- 选择结果显示实际好感/信任变化与事件原因。关系由数值、共同经历和双方明确选择推进，阶段为初识、熟悉、信任、亲近；恋人不能靠隐藏标签或刷分直接获得。拒绝和设置边界不扣分。
- 切回角色继续旧存档，不重放初次认识；`/story continue` 重发既有场景与新菜单码，不重复奖励。`/reset` 仍只重置普通聊天 session。

| 命令（所有玩家可用） | 作用 |
|---|---|
| `/story setup`（管理员） | 为上线前已在使用的角色补发公开人物简介、背景与玩法；已发送则不重复，投递未知需 `setup retry` 明确恢复 |
| `/story start` / `/story continue` | 开始 / 继续个人存档，选择后自动生成下一幕 |
| `/choose <菜单码> <编号>` | 选择本人菜单，拒绝过期、重复、跨会话或旧角色选项 |
| `/story status` / `/relation` | 当前角色关系、存档状态和最近变化 |
| `/story history` / `/story pause` | 已确认经历 / 暂停本人剧情 |
| `/story restart` | 给出5分钟确认码；确认后归档并重置本角色、本会话、本玩家的剧情与关系 |
| `/story forget` | 确认后清除对应活动/归档剧情内容、事件与派生数据；真实用户 facts 不变 |
| `/story boundaries` | 私聊查看/声明/撤回内容偏好；本期成人生成始终关闭 |

开局采用两阶段生成：先生成并验证人物/世界概要，再生成紧凑章节选择图；概要有检查点，图失败后重试不会重新抽背景。每阶段最多一次带具体错误路径的修复，排队、单次、阶段与总准备均有时间上限，场景使用独立较小输出预算。`output_mode` 默认兼容旧 `json_mode`；仅确认供应商支持后才选择 native strict schema。语法/Schema不代表剧情语义正确，发布仍做本地规则检查。

悬空引用、非法效果、权限/恋爱越级及没有安全保底选项继续硬拒绝；图规模建议、孤立但未引用节点和有限采样未覆盖路线记质量警告，不再用满分角色模拟当可达性证明。当前周目仍限制无环图，暂不支持循环。`/story refresh` 可按当前关系重新筛选同一场景的合法选项，文案/效果不变，新菜单码替代旧菜单；普通续档不重抽选项。

剧本版本固定，已展示选项的含义与效果固定。生成失败不消费选择、不修改关系；投递未知时冻结推进，不自动盲重发。明确 `/story continue` 或后台恢复可能重复展示文字，但不会重复加分。承诺/延迟后果按本玩家剧情回合触发，暂停或切卡期间不因群里其他人发言而推进。

### 角色差异和管理

后台可查看世界包、存档、事件、生成诊断和投递回执，入队恢复/重开、关联新旧卡片版本及导入旧分数。危险操作显示完整目标并要求输入目标 QQ 确认。详情分页读取，默认列表不含私密正文。保存设置由 bot worker 生效，无需重启。

卡片作者可在 `data.extensions.qqbot.galgame` 设置 `enabled`、`tone`、`pace`、`boundaries`（安全范围内）。后台按稳定 `character_id` 保存偏好；只影响未来的世界包，不改写已运行路线。同名卡不自动合并，新版本必须明确关联。旧卡生成稳定本地身份，fingerprint 仅代表内容版本。

新表在既有 `profile.db`，角色身份/切卡任务在 `characters.db`，自动进入现有 SQLite 备份。旧 `user_relations` 不广播复制给每个角色；故事开启时停止写入，管理员仅能明确导入到空关系档，恋爱确认与成人授权不迁移。关闭故事时保留旧模式与数据，不自动删档。

### 内容范围

本期仅日常、冒险、友情和非露骨恋爱。群聊始终安全模式；成年资格、角色设定、用户 opt-in、撤回和关系分数彼此独立。年龄声明不是身份验证，高好感和管理员操作不能代用户同意。权限/结构校验与词汇拦截是防线，不是全面语义安全保证；未开启显式内容生成。

独立剧情生成不读共享群历史、其他玩家存档或共享 notes，不把虚构往事写入真实用户 facts。遗忘只能删除本地存储；不能保证撤回已发送群消息、模型服务商副本和保留期内备份（默认14天）。

## 配置

| 路径 | 用途 |
|---|---|
| `/etc/qqbot/bot.env` | 密钥和运行开关，`640 root:qqbot` |
| `/etc/qqbot/config.toml` | OneBot、LLM、DSH、回复策略、路径 |
| `/var/lib/qqbot/dsh-home/profiles/qqbot/cordis.patch.yml` | DSH profile 的插件选择 |
| `/var/lib/qqbot/characters.db` | 角色卡库（SQLite） |
| `/etc/qqbot/character.json` | 旧版单卡文件，库为空时导入 |
| `/var/lib/qqbot/dsh-workspace/` | 模型可读写的记忆工作区 |

`bot/config.example.toml` 里有完整字段和注释。群聊默认只回复 @ 消息；不想在群里说话时把 `[bot] reply_group_at` 设为 `false`。

## 模型注册表

模型配置统一以 SQLite `/var/lib/qqbot/models.db` 为准，分成供应商和模型两级。

`providers`（连接信息，一个供应商一行）：

| 字段 | 含义 |
|---|---|
| `provider` | 主键，DSH route 名（如 `opencode-go`、`deepseek`） |
| `display_name` | 面板和 DSH 上显示的名字 |
| `api` / `base_url` | 协议和 OpenAI 兼容端点 |
| `api_key` | 面板里直接保存的密钥（DB 文件权限 600） |
| `enabled` | 是否参与渲染；禁用后该供应商的模型都不会写入 DSH route |

`models`（模型，主键 `(provider, model_id)`）：

| 字段 | 含义 |
|---|---|
| `provider` + `model_id` | 所属供应商，以及对应 DSH 的模型 id |
| `display_name` | 展示名 |
| `context_window` | 上下文长度，DSH 的 token meter 和压缩用它 |
| `max_output_tokens` | 单次输出上限；`0`（默认）表示不发送 `max_tokens`，使用提供商/模型默认 |
| `reasoning_effort` | 推理强度，留空表示用模型自己的默认值 |
| `capabilities` | JSON；`reasoning_efforts` 列出该模型接受的推理强度，`input` 声明图片输入 |
| `enabled` | 是否参与渲染 |
| `is_active` | 当前使用的模型，全表只能有一行为 1 |

Admin UI 的「模型」页按供应商 → 模型组织：先添加供应商（可从 OpenCode、DeepSeek、OpenAI、OpenRouter、SiliconFlow、百炼、Moonshot、智谱等预设开始，预设带品牌图标），点进供应商卡片后再添加该供应商的模型。API Key 必须直接在供应商表单里填写；保存后写入 `models.db`（`chmod 600`），面板不回显。修改已保存的 Key 后，机器人在下一轮消息自动重载 DSH。

首次启动写入默认供应商 `opencode-go` 和模型 `opencode-go/deepseek-v4.1-flash`（1M，默认激活）。默认数据只在首次初始化时写入，之后不会覆盖你的修改，删空的模型不会在重启后重新出现。旧版本的库会自动把每行重复的 `api` / `base_url` / `api_key_env` 拆分到 `providers`，并把旧的 `opencode/*` 默认行迁移到 `opencode-go/*`。

启动时机器人读取激活行，把它的 `max_output_tokens`（`0` 时省略）和 `reasoning_effort` 作为请求参数。机器人还会把所有启用的供应商和模型渲染成 DSH `llm-pi-ai` 的 patch（`/var/lib/qqbot/dsh-state/models.patch.json`），通过 SDK 的 `patches` 传给 DSH。数据库里的密钥不会写进 patch。机器人启动 DSH runtime 前把密钥放进一个按供应商生成的 `QQBOT_PK_*` 环境变量，patch 只引用这个名字；DSH 的 credentials 层按请求解析 `apiKeyEnv`。`reasoning_effort` 必须出现在该行的 `capabilities.reasoning_efforts` 里，否则写入时直接拒绝。内置 provider（`opencode` / `opencode-go`）只覆盖 `apiKeyEnv`、`baseURL` 和模型参数，**不写 `api`**，以免替换 DSH 自带的 adapter 包装（OpenCode Go 依赖它加 `x-opencode-session`）。

```bash
M="sudo -u qqbot /opt/qqbot/bot/venv/bin/python /opt/qqbot/bot/main.py"

$M --list-models                                              # 查看，* 为当前模型
$M --use-model opencode-go/deepseek-v4.1-flash                # 切换模型
$M --set-model opencode-go/deepseek-v4.1-flash reasoning_effort=medium max_output_tokens=4096
$M --set-model deepseek/deepseek-chat \
   base_url=https://api.deepseek.com api_key_env=DEEPSEEK_API_KEY \
   context_window=1000000 'capabilities={"reasoning_efforts":["low","high"]}'   # 新增供应商 + 模型
```

`--set-model` 仍兼容旧的 `base_url` / `api` / `api_key_env` 参数：如果供应商不存在会一并创建，已存在则更新该供应商的连接信息（影响它下面的所有模型）。也可以直接在 Admin UI 里操作。

`[llm]` 段只剩 `temperature` / `timeout`，用于 `[dsh] enabled = false` 时的回退客户端；DSH SDK 不开放 temperature。

## 上下文与压缩

- **上下文长度**来自模型注册表的 `context_window`：付费 Flash 是 1M，free 是 200k。OpenCode 的 `/models` 不返回 context length，所以必须显式登记。模型不需要在 prompt 里被告知上下文长度；DSH 的 token meter/compaction 直接读 route 元数据。
- `DSH_CONTEXT_WINDOW` 环境变量只用于直连 DeepSeek adapter 的兜底，和本项目的 `llm-pi-ai` route 无关。
- `compaction-basic` 默认在 80% 压力时自动压缩。本项目显式配置为：

```yaml
thresholdRatio: 0.8        # 目标 80%
headroomTokens: 16384      # 额外预留
retainRatio: 0.16          # 压缩后保留约 16% 的最近上下文
auto: true
```

- 实际触发点 = `min(0.8 × contextWindow, contextWindow − 输出预留 − headroomTokens)`。显式设置 `max_output_tokens` 时用它作为输出预留；默认 `0` 时交给 DSH/提供商默认。1M / 200k 窗口按 80% 阈值约为 800k / 160k。
- 压缩会把较旧的历史总结成摘要，保留最近的上下文；完整 session 仍以 JSONL 存在 `/var/lib/qqbot/dsh-home/sessions`。
- 摘要默认使用同一个 provider/model；需要的话可以在 `compaction-basic` 里用 `summarizationProvider` / `summarizationModel` 单独指定。
- 非 0 的 `max_output_tokens` 是单次输出上限，也会影响压缩时的输出预留；`0` 表示使用提供商默认。

## 安全

- **QQ 账号风险**：NapCat 是第三方 NTQQ 客户端，可能触发风控甚至封禁；请使用小号。
- **不要暴露端口**：NapCat WebUI 和 OneBot WS 只绑 `127.0.0.1`。需要远程看 WebUI 时用 SSH 隧道，不要把端口改成 `0.0.0.0`。
- **无 shell**：DSH profile 禁用了 bash / pwsh / PTY；文件工具限制在 workspace，systemd 还有 `ProtectSystem=strict` 和 `ReadWritePaths=/var/lib/qqbot`。
- **数据外发**：聊天内容、工具结果和画像会发给模型提供商；不要把密钥、隐私文件放进 workspace。
- **DSH preview**：插件 API 可能变化，升级前先看 release notes；本项目固定 `0.2.1rc2`。

部署安全说明见 [SECURITY.md](SECURITY.md)。

## 目录结构

```
bot/        adapter：OneBot 客户端、DSH client、角色卡、SQLite、prompt 回退
cards/      示例角色卡（安装脚本复制到 /opt/qqbot/cards）
deploy/     NapCat/Docker、systemd、安装脚本、诊断工具
dsh/        DSH 自定义 profile patch、user-profile / session-history 插件、fake LLM 测试服务
docs/       架构、运维与音色说明（architecture.md / ops.md / voices.md）
tests/      单元测试（解析、存储、命令、面板 API 等）
```

## 开发

```bash
uv venv
uv pip install -r bot/requirements.txt -r requirements-dev.txt
source .venv/bin/activate          # Windows: .venv\Scripts\activate

python -m unittest discover -s tests -v
ruff check .
pyright
```

`ruff` 和 `pyright` 的配置都在 `pyproject.toml`。Pyright 使用 `standard` 检查模式（`typeCheckingMode = "standard"`；包本身没有名为 `standard` 的 extra）。测试里的 `SimpleNamespace` / `**overrides` 打桩在 `[tool.pyright.executionEnvironments]` 中单独放宽，生产代码仍按标准模式检查。

本地调试 DSH 工具链时，可以不用真实模型：`dsh/fake_llm.py` 是一个 OpenAI-compatible 的假服务，会先让模型调用 `profile_write`，再返回文本。profile patch 里已经带了一个指向 `127.0.0.1:8000` 的 `fake` 路由。

## 运维与排障

服务单元、路径、日志、备份/恢复和常见故障的处理步骤见 [`docs/ops.md`](docs/ops.md)。日常检查用这三条命令：

```bash
systemctl status qqbot qqbot-admin          # 主进程和面板
journalctl -u qqbot -f                      # 主进程日志
sudo -u qqbot /opt/qqbot/bot/venv/bin/python /opt/qqbot/bot/main.py --check-onebot
```

## Roadmap

定位是社交娱乐型 QQ bot，功能不会很多。已经做完的语音、梗图、主动接话、长期记忆不再列出，接下来可能做：

- **群聊小游戏**：猜数字 / 成语接龙 / 海龟汤，状态按群隔离，输赢接到关系系统；
- **记忆检索优化**：剧情已有结构化事件和承诺，后续改进超长路线摘要与相关性检索；
- **运维可观测性**：面板里查看最近日志和任务/订阅的失败趋势，减少登录服务器排障。

## License

MIT

## 视频链接解析

DSH 的 `video_resolve` 工具接受一个 `source`（BV/AV、视频 URL 或含一个链接的分享文案）和可选 `part`。B 站支持普通视频、`b23.tv` 短链和指定分 P；默认仅第一 P，不展开合集。抖音支持单视频页面和 `v.douyin.com` 短链，属于实验功能，默认关闭；图集、直播、番剧及原生 QQ 视频附件不在本期范围内。

返回统一 `schema_version: 1` 结果：平台/内容 ID、规范网页地址、标题、作者、简介、时长、封面、选中分 P、最多 12 项音视频资源描述和警告。**不会下载视频、提供播放直链、转写、总结或发送 QQ 视频；元数据不代表模型看过视频。** 字幕未查询时为 `unknown`，不把弹幕当字幕。

配置位于 `config.toml` 的 `[video]`，旧配置缺少此节也能运行：

```toml
[video]
enabled = true
bilibili_enabled = true
douyin_enabled = false
timeout = 30
concurrency = 2
calls_per_turn = 3
cache_seconds = 300
cache_entries = 128
bilibili_cookies = ""
douyin_cookies = ""
```

Cookie 路径仅由运维配置，使用平台专属 Netscape 文件，放在 workspace 之外并限制文件权限；程序不读取浏览器 Cookie、不自动登录，也不写回文件。不要把 Cookie 发进聊天。抖音可能需要新鲜 Cookie（不一定需要已登录账号）；B 站 ECS 出口也可能遭风控，解析器支持不等于部署环境可用。

失败返回 `status: "error"` 和 `error.code/message`：例如 `INVALID_SOURCE`、`UNSUPPORTED_SOURCE`、`AUTH_REQUIRED`、`CREDENTIALS_REQUIRED`、`CREDENTIALS_INVALID`、`ACCESS_RESTRICTED`、`RATE_LIMITED`、`UPSTREAM_BLOCKED`、`NOT_FOUND`、`TIMEOUT`、`EXTRACTOR_BROKEN`、`BACKEND_UNAVAILABLE`、`LIMIT_EXCEEDED`、`BUSY`。不能可靠区分凭据失效和平台风控时不猜测原因，也不尝试绕过。

工具仅对进入普通回复流程的真实用户消息开放；不自动抓取所有群消息，不给定时/主动/合成消息授权，DSH 关闭时不提供该工具。视频权限与管理员角色工具分离。首版复用单轮独立 DSH runtime，因而每个视频启用的聊天轮次都有运行时重启开销，但磁盘会话历史保留。可用 `enabled = false` 关闭。

后端固定 `yt-dlp==2026.10.26`；升级须同时更新版本校验、通过网络/提取器回归测试并在实际部署出口验证，不在运行时自动升级。离线实际 SDK 冒烟测试：

```bash
python scripts/check_video_tools_runtime.py
python -m unittest discover -s tests -p 'test_video*.py' -v
```

冒烟脚本只调用本机假模型和假解析结果；单元测试通过真实固定版提取器处理固定 HTTP 响应，不代表线上平台一定成功。实际平台验证应另行报告，无 Cookie 时如实保留错误，不为测试收集凭据。
