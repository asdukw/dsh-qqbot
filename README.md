# DSH QQ Bot

> 一个把 **DeepSeek Harness（DSH）** 接到 **QQ** 上的聊天机器人。NapCat 负责 QQ 协议，DSH 负责 agent runtime、会话记忆、文件工具和可插拔的画像插件。

项目还处于早期阶段。DSH 本身是 developer preview，当前固定 `0.2.1rc2`，升级可能带来不兼容变更。

## 特性

- **QQ 私聊聊天**，群聊默认只回复 @ 消息（`reply_group_at = false` 可关闭）
- **赛程**：赛程类回答由模型输出 ```schedule JSON，adapter 用 Pydantic 校验后渲染成固定格式（阶段/北京时间/对阵/BO），校验失败不会把半成品发出去；
- **语音回复**：模型在短回复末尾写 `[语音]`，按 provider（edge / StepFun / Fish Audio）合成并发送 QQ 语音；主动接话按 `[voice] probability` 概率转语音。中文音色清单见 [`docs/voices.md`](docs/voices.md)（`scripts/export_fish_voices.py` 可重新导出）；
- **梗图**：模型写 `[梗图: 文字]`，从表情库里挑一张当底图、加字幕后发出去；
- **长期记忆**：`memory.db` 的 FTS5 trigram 索引会在回复前检索当前会话的相关历史，作为 `[相关历史]` 注入；
- **订阅转发**：`/sub add <B站UID|空间链接|RSS>` 把 UP 动态或任意 RSS 转到当前群/私聊；B 站直连被机房 IP 风控，默认走可配置的 RSSHub 镜像；
- **戳一戳**：被戳会像被叫到一样回应（8 秒冷却）；群里别人被戳会作为一条上下文 `[戳了戳 某人]` 记下来；
- **定时任务**：管理员直接说"每天早上8点提醒我起床"即可创建，说"把喝水那个删了/暂停"也能管理（模型输出 `[定时: ...]` / `[定时列表|删除|暂停|恢复]`，adapter 解析 + Pydantic 校验）；任务绑定创建时的群/私聊，可显式选 here/private/group；支持 fixed/generate 两种文案（generate 到点按人设现写）；`/task list|delete|pause|resume|run`；
- **每日日报**：每天 24:00 给当天活跃的群发一份趣味总结——水王榜、首条/末条、热词、复读现场、今日金句、今日宜忌，再加一段按角色卡口吻写的锐评；私聊可在 `[daily]` 里开关；`/daily [YYYY-MM-DD]` 可手动预览/补发；
- **联网搜索**：DSH 官方的 `web_search` / `web_fetch` 工具；搜索 provider 用免密钥后端（与 OpenCode 自带 websearch 同类），不需要额外 API key；
- **角色卡**：SillyTavern Character Card V1 / V2 / V3，支持 `.json` 和 `.png`（读取 `ccv3` → `chara` → `iTXt`）；SQLite 角色库，`/card search/install/use` 热切换
- **DSH session**：每个 QQ 会话一份持久化 JSONL 历史，自动上下文压缩；`session-history` 插件让模型按需读取旧 generation
- **用户画像**：内置 `user-profile` 插件，SQLite 存储，提供 `profile_read` / `profile_write` / `profile_forget`
- **文件记忆**：模型可使用 `read` / `write` / `edit` / `glob` / `grep`，工作目录限定在 `/var/lib/qqbot/dsh-workspace`
- **无 shell**：DSH profile 中禁用了 bash / pwsh / PTY / jobs，只保留文件与搜索工具
- **OneBot 正向 WS**：带 token 鉴权、断线重连、消息去重、长回复自动拆分
- **systemd 常驻**

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

更细的说明见 [`docs/architecture.md`](docs/architecture.md)。

## 快速开始（Ubuntu 24.04+ / Debian 13+，需要 Docker Compose v2）

### 0. 准备

- Linux 主机，1 核 2G 内存起步（加 2G swap 更稳）
- Docker + Compose
- Python 3.12+
- 一个 **QQ 小号**（强烈建议，不要用主号）
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
```

说明：

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

## 管理员命令

管理员 QQ 号写在 `ADMIN_QQ_IDS`（逗号或空格分隔，默认读 `/etc/qqbot/bot.env`）；非管理员发送的 `/` 消息会被静默忽略。

- `/model`：列出模型及其序号；
- `/model <序号|model_id|provider/model>`：切换激活模型，立即热切换，无需重启；
- `/card` 系列：角色卡库的搜索、安装、切换、删除（见[角色卡](#角色卡)）；
- `/reset`（或 `/new`）：归档当前 DSH 会话，下一条消息开始新会话；
- `/daily [YYYY-MM-DD]`：立即生成当前会话的每日日报（默认昨天，用于预览或补发）；
- `/help`：显示帮助。

命令不会写入对话历史，也不会送进模型。

## Admin UI

面板是 `admin/` 下的 Vite + React + Tailwind 应用，组件按 shadcn/ui 的方式直接放在 `src/components/ui/`，主题为蓝白。页面按内容分组，左侧导航 + hash 路由，每个功能独立成页：

- **仪表盘**：今日消息 / Token、7 / 30 天趋势、订阅异常、最近消息、当前模型与角色卡；
- **对话**：聊天分析（今日发言排行、词云、会话分布）；词云用前端 SVG 螺旋布局，字号 = 覆盖消息数，不依赖额外图表库；
- **自动化**：定时任务、订阅推送；
- **内容**：模型（供应商 → 模型两级管理，可新增、编辑、启停、切换当前模型）、角色卡、表情库；
- **用量**：Token 用量，支持 7 / 30 / 90 天与按会话筛选；
- **系统**：数据库路径与大小、DSH 路径、面板配置。

Python 端只提供 JSON API 和静态文件：

- 本地构建：`cd admin && npm install && npm run build`；产物在 `admin/dist/`（已随仓库提交，便于纯 Python 部署）。
- 服务端：`bot/admin_ui.py` 默认从 `/opt/qqbot/admin/dist` 提供页面。读取接口：`/api/session`、`/api/login`、`/api/logout`、`/api/overview`、`/api/models`、`/api/cards`（点开单张卡时用 `/api/cards/<id>` 取完整 V3 payload）、`/api/tasks`、`/api/feeds`、`/api/stickers`、`/api/system`、`/api/usage`、`/api/fun`；`/api/dashboard` 保留为旧版聚合接口。模型页的写入接口（需登录、JSON、同源）：`POST /api/models/providers`、`/api/models/providers/delete`、`/api/models/items`、`/api/models/items/delete`、`/api/models/active`。登录仍用 `ADMIN_UI_PASSWORD` + HMAC cookie。
- 配置：`[admin] static_dir`；部署后 `systemctl restart qqbot-admin`。
- 模型页可写，其余页面只读；每 60 秒自动刷新，右上角「刷新」会立即重拉当前页。API Key 直接在供应商表单里填写，保存在 `models.db`（权限 600）且不会回显。

## Token 审计与今日趣味数据

- `usage.db` 记录每次模型调用的非缓存输入、缓存输入和输出：DSH 的每个 assistant step、compaction summary，以及图片描述、表情打标、任务解析/文案等 thin client 调用。
- Admin UI 的「Token 用量」支持 7/30/90 天和按群聊/好友筛选，展示缓存命中率、按用途、按 session 的排行。
- `memory.db` 的 `chat_events` 记录每条群聊/私聊入站消息；Admin UI 的「今日发言排行」「今日词云」按当天数据生成。词云按业界常用做法处理：jieba 词性标注后只保留名词/动词/形容词/成语/英文，丢弃代词、连词、介词、数词、量词、语气词等；再排除成员昵称、停用词和纯语气词。`count` 是「提到该词的消息数」而不是原始词频，同一条消息里重复只算一次；长区间用 `ROW_NUMBER()` 均匀抽样（最多 2000 条），并默认过滤只出现一次的词。未安装 jieba 时回退到正则分词 + 停用词。
- 配置在 `[usage]`：`database`、`retention_days`、`event_retention_days`。

## 每日日报

每天 00:00（`[daily] hour/minute`，Asia/Shanghai）给「前一天有消息的会话」发一份日报；默认只发群，`private = true` 时私聊也发。

- **统计是确定性的**：消息数、参与人数、活跃度星级、发言排行（水王/亚军/季军）、首条与末条、热词（按覆盖消息数，jieba/正则）、复读现场（同一句话由至少两个人连续刷 ≥ `min_repeat` 次）、随机金句、bot 自己的回复数、按日期和会话固定种子的「今日宜/忌」。
- **锐评可选**：一次 thin client 调用，把统计和聊天节选交给当前模型，按角色卡口吻写 2-4 句；失败或超时就只发统计块，不影响日报本身。调用记录在 `usage.db`（purpose `daily_summary`）。
- **不污染会话**：日报走 OneBot 直接发送，不写入 DSH session / `messages` 历史，也不进入模型上下文。
- **去重与补发**：`daily.db` 记录每个 `(会话, 日期)` 的状态（`sent` / `skipped` / `failed`）；重启后在 `max_late_hours`（默认 6 小时）内会自动补发，已发送的不会重复。少于 `min_messages` 的会话标记为 skipped，不打扰冷清群。
- **配置**：`database`、`enabled`、`hour`、`minute`、`groups`（空 = 所有群）、`private`、`min_messages`、`max_late_hours`、`check_seconds`、`sample_chars`、`max_chars`、`max_attempts`、`min_repeat`、`prompt`（追加的风格提示）。
- **手动**：管理员在任意群/私聊发 `/daily` 生成昨天的日报，或 `/daily 2026-10-24` 指定日期。手动生成会忽略 `min_messages`。

## 角色卡

角色卡统一入库到 SQLite `/var/lib/qqbot/characters.db`，内部一律存为 V3 结构（V1 平铺字段和 V2 的 `chara_card_v2` 都会自动升级）。管理员可以直接在聊天里管理：

| 命令 | 作用 |
|---|---|
| `/card` | 列出角色卡，`*` 为当前卡 |
| `/card search <关键词>` | 搜索名称、昵称、简介、性格、场景、标签、作者；空格分词按 AND 处理 |
| `/card install <url\|路径>` | 安装 JSON / PNG 卡，支持 `http(s)://`、`file://` 和服务器绝对路径 |
| `/card install`（附图片） | 把管理员发送的 PNG 卡图片直接安装 |
| `/card use <id\|名称>` | 切换当前卡，立即生效，DSH session 历史保留 |
| `/card show <id\|名称>` | 查看来源、标签、作者、简介长度和世界书条目数 |
| `/card delete <id\|名称>` | 从库中删除（当前卡需先切换） |
| `/card alias` | 查看当前卡的名称与别名 |
| `/card alias add <名称>` | 添加别名：群里直接叫它（至少 2 个字）也会被当成在找它 |
| `/card alias remove <名称>` | 删除别名 |
| `/sticker` | 列出最近收藏的表情包（群里收到的表情会自动入库） |
| 入库去重 | 先按 SHA1 精确去重，再用 64-bit dHash 近似判重；重编码/轻微压缩的同图会复用已有记录 |
| `/sticker search <关键词>` | 按描述/标签/来源搜索 |
| `/sticker add`（附图） | 把管理员发的图片收藏进表情库 |
| `/sticker import` | 导入 QQ 账号里的收藏表情 |
| `/sticker delete <id>` / `ban` / `unban` | 删除或禁用/恢复 |
| `/sticker stats` | 数量与占用空间 |
| `/sticker tag <id> <描述> [标签,标签]` | 手动补描述/标签，发送时按语境挑 |
| `/sticker send <id>` | 手动发一张，验证发送链路 |
| `/task add <一句话>` | 语义化创建定时任务（仅管理员），如 `每天早上8点提醒大家喝水` |
| `/task` / `/task list` | 列出任务、下次执行时间和目标会话 |
| `/task delete\|pause\|resume\|run <id>` | 删除 / 暂停 / 恢复 / 立即执行一次 |

命令行等价操作：

```bash
M="sudo -u qqbot /opt/qqbot/bot/venv/bin/python /opt/qqbot/bot/main.py"

$M --list-cards
$M --search-cards 图书
$M --install-card https://example.com/card.png
$M --use-card 2
$M --add-card-alias 肥鱼
$M --remove-card-alias 肥鱼
```

- 按内容指纹去重：同一张卡重复安装会返回已有记录，不会产生副本；
- 别名按卡保存（`cards.aliases`），和卡片自带的 `name` / `nickname` 一起参与「叫名字」判定；单字别名会被忽略；
- 下载上限 12 MB；只接受 PNG 或 JSON，PNG 依次查找 `ccv3`、`chara` 的 `tEXt` / `iTXt` 块；
- 卡里的 `description` / `personality` / `scenario` / `mes_example` / `system_prompt` 会渲染成 DSH 的系统提示词；
- 世界书条目会原子写入 `memory/lore.md`，模型用 `grep` / `read` 查；
- 换卡会让 DSH runtime 重启，但 session 历史保留；
- **升级兼容**：库为空时自动导入 `[cards] bootstrap`（默认 `/etc/qqbot/character.json`）。只要这张卡仍是当前卡，修改文件仍会热加载；切到别的卡后文件不再影响运行中的机器人。
- 仓库自带一张社区整理的示例卡 [`cards/dafeiyu.json`](cards/dafeiyu.json)（「大肥鱼」，DeepSeek 二创鲸鱼娘）；安装脚本会把它放到 `/opt/qqbot/cards/`，用 `--install-card` 或 `/card install` 入库。

## 记忆

四层：

1. **会话记忆**：DSH session JSONL，位于 `/var/lib/qqbot/dsh-home/sessions`，私聊按人、群聊按「群 + 人」各一份，自动压缩。
2. **用户画像**：SQLite `/var/lib/qqbot/dsh-state/profile.db` 的 `user_facts`，**按 QQ 号**存，私聊和群聊共用一份。每条事实带可见范围：私聊里得知的默认 `private`，只在私聊注入，群里看不到也改不了；群里得知的是 `public`。工具：`profile_read` / `profile_write` / `profile_forget`。
3. **群画像**：同库的 `group_facts`，**按群号**存，记群主题、氛围、常聊话题、梗和黑话、群规、约定。只在该群里注入。工具：`group_profile_read` / `group_profile_write` / `group_profile_forget`（私聊里不可用）。
4. **文件记忆**：`/var/lib/qqbot/dsh-workspace/memory/`；`lore.md` 是角色设定，`notes/` 放事件、约定和重要片段。模型可以用 `write` / `edit` / `glob` / `grep` 维护和检索。

会话 JSONL 不在文件工具的工作区内，所以另有一个只读的 **`session-history` 插件**，只在模型主动调用时读取，不会每轮注入：

- `history_sessions`：列出当前群/私聊的 session 文件（当前 + 旧 generation，含起止时间、轮数、大小）；
- `history_read`：读取某个 session 的 JSONL，返回用户消息和助手回复，支持关键词、`since` / `until`、条数限制；
- `chat_search`：检索 `memory.db.chat_events` 的入站消息，包含群里未 @ 机器人的普通消息（保留 90 天，不含助手回复）。

三个工具都只读，并且只允许访问当前群/私聊，不能跨会话读取。模型在用户问到“昨天/之前聊了什么”时会先查再答。

### 群聊上下文与活跃度

群里没 @ 机器人的消息会按群留在内存里最近 `group_context_messages`（默认 30）条；被 @ 时，这些消息和 @ 的那句一起交给模型，它就能接上话题。每条只送一次，不写盘，不跨群。

机器人会在这些情况下开口：

- **被 @**：直接回复；
- **被名字直呼**：消息里出现当前角色卡的 `name` / `nickname`（切卡后自动跟着变），或 `[bot.proactive] aliases` 里的额外名字，也算被叫到，直接回复；
- **引用回复**：引用了机器人最近发的消息（保留最近 200 条消息 id），也直接回复；
- **主动接话**：其余消息按 `[bot.proactive]` 判定——`probability`（默认 15%）逐条掷骰，命中后还要满足冷却 `cooldown_seconds`（默认 90s）、小时/天配额（12/60）、最少上下文条数、安静时段；`keywords` 命中时跳过概率直接尝试；
- **接话窗口**：机器人刚在群里说过话后 `followup_seconds`（默认 300s）内，概率乘以 `followup_multiplier`（默认 2），更容易顺着话题继续聊。

`/proactive status` 可以查看当前概率、冷却、配额和本群用量；`/proactive on|off` 临时开关。

### 身份如何传给 DSH

DSH SDK 的 prompt 接口只有 session id 和消息内容，没有元数据字段。所以 adapter 每轮调用前，把这个 session 的 `scope`（private/group）、QQ 号、群号、群名片写进 `profile.db` 的 `conversations` 表（`bot/conversations.py`），插件按 session id 查这张表拿身份，**不解析 session id**。查不到身份的会话，画像工具会直接报错。

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

Admin UI 的「模型」页按**供应商 → 模型**组织：先添加供应商（可从 OpenCode、DeepSeek、OpenAI、OpenRouter、SiliconFlow、百炼、Moonshot、智谱等预设开始，预设带品牌图标），点进供应商卡片后再添加该供应商的模型。API Key 必须直接在供应商表单里填写；保存后写入 `models.db`（`chmod 600`），面板不回显。修改已保存的 Key 后，机器人在下一轮消息自动重载 DSH。

首次启动写入默认供应商 `opencode-go` 和模型 `opencode-go/deepseek-v4.1-flash`（1M，默认激活）。默认数据只在首次初始化时写入，之后不会覆盖你的修改，删空模型也不会在下次重启时复活。旧版本的库会自动把每行重复的 `api` / `base_url` / `api_key_env` 拆分到 `providers`，并把旧的 `opencode/*` 默认行迁移到 `opencode-go/*`。

启动时机器人读取激活行，把它的 `max_output_tokens`（`0` 时省略）/ `reasoning_effort` 作为请求参数，并把所有启用的供应商和模型渲染成 DSH `llm-pi-ai` 的 patch（`/var/lib/qqbot/dsh-state/models.patch.json`），通过 SDK 的 `patches` 传给 DSH。数据库里的密钥不会写进 patch：机器人启动 DSH runtime 前把它放进一个按供应商生成的 `QQBOT_PK_*` 环境变量，patch 只引用这个名字；DSH 的 credentials 层按请求解析 `apiKeyEnv`。`reasoning_effort` 必须出现在该行的 `capabilities.reasoning_efforts` 里，否则写入时直接拒绝。内置 provider（`opencode` / `opencode-go`）只覆盖 `apiKeyEnv`、`baseURL` 和模型参数，**不写 `api`**，以免替换 DSH 自带的 adapter 包装（OpenCode Go 依赖它加 `x-opencode-session`）。

```bash
M="sudo -u qqbot /opt/qqbot/bot/venv/bin/python /opt/qqbot/bot/main.py"

$M --list-models                                              # 查看，* 为当前模型
$M --use-model opencode-go/deepseek-v4.1-flash                # 切换模型
$M --set-model opencode-go/deepseek-v4.1-flash reasoning_effort=medium max_output_tokens=4096
$M --set-model deepseek/deepseek-chat \
   base_url=https://api.deepseek.com api_key_env=DEEPSEEK_API_KEY \
   context_window=1000000 'capabilities={"reasoning_efforts":["low","high"]}'   # 新增供应商 + 模型
```

`--set-model` 仍兼容旧的 `base_url` / `api` / `api_key_env` 参数：如果供应商不存在会一并创建，已存在则更新该供应商的连接信息（影响它下面的所有模型）。更直观的方式是使用 Admin UI。

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

更完整的部署安全说明见 [SECURITY.md](SECURITY.md)。

## 目录结构

```
bot/        adapter：OneBot 客户端、DSH client、角色卡、SQLite、prompt 回退
cards/      示例角色卡（安装脚本复制到 /opt/qqbot/cards）
deploy/     NapCat/Docker、systemd、安装脚本、诊断工具
dsh/        DSH 自定义 profile patch、user-profile / session-history 插件、fake LLM 测试服务
docs/       架构说明
tests/      单元测试（卡片解析、prompt）
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

## Roadmap

定位是社交娱乐型 QQ bot，后续功能不会很多，可能包括：

- 群聊互动 / 小游戏
- 表情、图片、语音消息
- 主动搭话 / 定时问候
- 更丰富的长期记忆（跨会话画像、事件检索）

## License

MIT
