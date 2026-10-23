# 架构

## 组件

```
┌──────────────┐      OneBot 11 WS       ┌──────────────────────┐
│   NapCat     │ ◄─────────────────────► │  qqbot adapter       │
│  (Docker)    │   127.0.0.1:3001        │  (Python / systemd)  │
└──────────────┘                         └──────────┬───────────┘
                                                    │ Python SDK
                                                    ▼
                                         ┌──────────────────────┐
                                         │  DSH runtime         │
                                         │  profile: qqbot      │
                                         └──────────┬───────────┘
                                                    │
                              ┌─────────────────────┼─────────────────────┐
                              ▼                     ▼                     ▼
                       llm-pi-ai route       session + compaction    user-profile
                       (OpenCode Go)        (JSONL, 自动压缩)        (SQLite)
```

### adapter（`bot/`）

职责：

- 维护到 NapCat 的 OneBot 11 正向 WebSocket：token 鉴权、重连、心跳、echo 关联；
- 解析消息段（text / at / image / face / record 等），去重；
- 决定是否回复：私聊全回；群聊在被 @、叫当前角色名（卡片的 `name`/`nickname` 或 `aliases`）或引用机器人消息时回复，其余消息按主动策略参与；
- 把 QQ 会话映射成稳定的 DSH `session_id`；
- 角色卡库（SQLite）与热切换、系统提示词构建、世界书落盘；
- 长回复拆分、发送失败日志；
- 本地 SQLite 消息审计（不是 DSH 的上下文来源）。

不负责：模型调用、历史管理、工具执行、画像存储。这些在 DSH 里。

### DSH profile（`dsh/qqbot.cordis.patch.yml`）

从 DSH 的 `sdk-minimal` 模板派生，保留：

- `sdk-app-startup` / `sdk-jsonrpc-server`
- `llm-pi-ai`（统一模型接入；OpenCode Go 路由）
- `llm` / `llm-retry` / `agent` / `agent-loop`
- `session` / `session-projection` / `sessions`（JSONL 持久化）
- `token-meter` / `compaction-basic`
- `system-prompt` / `tools`
- `filesystem` / `sandbox` / `sandbox-policy`
- `tool-fs`（read/write/edit）
- `tool-fs-search`（glob/grep）
- `subprocess`（仅供 `tool-fs-search` 启动打包的 `rg`）
- `fs-observation-policy`
- `user-profile`（自定义插件）
- `web` + `tool-web` + `web-fetch-http`（官方 web seam 与 `web_search` / `web_fetch` 工具）
- `web-search-keyless`（自定义 provider，注册到官方 `ctx.web`；官方 exa/perplexity/deepseek provider 都要各自的 key，这个走免密钥搜索后端）

禁用：

- `terminal-bash` / `terminal-pwsh` / `persistent-bash` / `persistent-pwsh` / `pty`
- `jobs`
- `tool-working-directory`
- `session-title`（省一次模型调用）
- DeepSeek 专属的 `llm-deepseek` / `deepseek-llm-api-extensions` / `plugin-package-inventory-deepseek`
- `session-log-deepseek` 的上传（`enabled: false`）

沙箱策略：

```yaml
mode: workspace-write
workspaceRoot: /var/lib/qqbot/dsh-workspace
```

systemd 再叠加一层：`ProtectSystem=strict`、`ReadWritePaths=/var/lib/qqbot`。所以即使 DSH 的文件工具被误用，也只能写 `/var/lib/qqbot`。

## 一条消息的路径

1. NapCat 通过 WS 推送 `message` 事件。
2. adapter 提取文本、判断是否需要回复：
   - 私聊：生成 `conversation_id = private:<user_id>`；
   - 群聊被叫到（被 @、消息里出现角色卡的名字、或引用了机器人最近发的消息）：生成 `conversation_id = group:<group_id>`，直接回复；
   - 其余群消息：放进该群的内存缓冲（`bot/group_context.py`，默认最近 30 条；图片消息同时保留图片 URL），再交给 `[bot.proactive]` 判定是否主动接话（概率、冷却、小时/天配额、安静时段、关键词、接话窗口）。
3. `session_id = conversation_id.replace(":", "-")`。
4. 读取角色卡库的 active signature（`id` / `updated_at` / `fingerprint`）；变化时重新生成系统提示词、原子写 `memory/lore.md`、重启 DSH runtime（session 保留）。`/card use` 会直接触发这次刷新；`[cards] bootstrap` 文件被修改时也会被检测到。
5. 把身份写进 `profile.db` 的 `conversations` 表：`session_id → scope, user_id, group_id, sender_name`（`bot/conversations.py`）。
6. 群聊时取出并清空该群缓冲，和被叫到的那句拼成一条用户消息：`[群聊里最近的消息] … [有人 @ 你] 昵称(QQ号): 内容`；每行都带发言者 QQ 号（群名片相同的人也能区分），消息头的发言者若还有不同的 QQ 昵称会额外注明。
7. 图片：模型能直接读图，但只有 thin client 支持 `image_url` 内容块。被叫到的消息自带图片时走 `_reply_llm`，并把群里最近 `group_context_images`（默认 2）张图片一起以 data URI 附上；DSH 的 content-block 协议不支持图片，所以主动接话先用一次视觉调用把最近图片转成一两句文字描述，再拼进 prompt。
8. `asyncio.to_thread(dsh.run, session_id, prompt)`：
   - DSH 启动/复用 runtime；
   - 加载或创建 `session_id` 的 JSONL 历史；
   - 组装 system prompt（插件按 `conversations` 查身份，注入用户画像和群画像）；
   - 跑 agent loop；
   - 如果模型调用画像工具或文件工具，执行并把结果回灌；
   - 返回 `final_response`。
9. adapter 按 `max_reply_chars` 拆分并发送。
10. 用户消息和助手回复同时写入本地 SQLite `messages` 表，仅作审计。

## 身份传递

DSH SDK 的 `session/prompt` 只接受 `sessionId` 和 `contentBlocks`，没有元数据字段，插件拿到的只有 `agent.id`（即 session id）。因此身份走旁路：adapter 在每轮调用前写 `conversations` 表，插件在渲染 system prompt 和执行工具时按 session id 查询。插件不解析 session id 的格式；查不到身份时画像工具直接报错，system prompt 里画像显示为空。

两边共用同一个 SQLite 文件，WAL 模式加 `busy_timeout`，adapter 写入发生在 DSH 读取之前，没有并发写同一行的情况。

## 画像插件（`dsh/user-profile.mjs`）

- 在 `agent/created` 里为每个 agent 注册 **agent-scoped** 的变量 `user_profile`、`group_profile` 和 section `user-profile`（order 8000），只影响当前 agent。
- 用户画像 `user_facts`，主键 `(user_id, key)`，**按 QQ 号**，私聊和群聊共用：
  - `visibility`：私聊写入默认 `private`，可显式设为 `public`；群聊写入一律 `public`；
  - 群聊里只读、只注入、只删 `public` 事实，且不能用公开值覆盖同 key 的私密事实；
  - 私聊注入时私密事实带「（私密）」标记，提醒模型别带到别处。
- 群画像 `group_facts`，主键 `(group_id, key)`，**按群号**：主题、氛围、常聊话题、梗和黑话、群规、约定；只在该群注入，`group_profile_*` 工具在私聊中拒绝执行。
- 迁移：v1 的 `facts`（按 session 存）在启动时按 `conversations` 映射到 `user_facts`——私聊来源记为 `private`，群聊来源记为 `public`；全部映射完后删除旧表，映射不了的行保留不动。

```sql
CREATE TABLE conversations (
  session_id  TEXT PRIMARY KEY,
  scope       TEXT NOT NULL CHECK (scope IN ('private', 'group')),
  user_id     TEXT NOT NULL,
  group_id    TEXT,
  sender_name TEXT NOT NULL DEFAULT '',
  updated_at  INTEGER NOT NULL
);
CREATE TABLE user_facts (
  user_id    TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,
  visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
  source     TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, key)
);
CREATE TABLE group_facts (
  group_id   TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,
  source     TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (group_id, key)
);
```

画像只保存明确说出的稳定事实。事件、约定和重要片段走文件记忆（`memory/notes/`），由模型用 `write` / `edit` / `glob` / `grep` 维护。

## 模型注册表（`bot/models.py`）

模型配置的唯一来源。代码和环境变量里都不再有模型 id、上下文长度、输出上限或推理强度。

- SQLite `/var/lib/qqbot/models.db`，表 `models`，主键 `(provider, model_id)`；字段：`display_name`、`api`、`base_url`、`api_key_env`、`context_window`、`max_output_tokens`、`reasoning_effort`（默认 `low`）、`capabilities`（JSON，含 `reasoning_efforts`）、`enabled`、`is_active`。
- `is_active` 上有部分唯一索引，保证全表最多一行激活；`use()` 在一个事务里切换。
- 空库时写入默认行（`opencode-go` 付费 Flash 1M 激活、free 200k、本地 fake）；之后不覆盖用户修改。旧版本库启动时 `_migrate()` 补列、给默认行补上 `reasoning_efforts`，并把旧的 `opencode/*` 默认行迁移到 `opencode-go/*`。
- 写入校验：新行必须有 `base_url` 和 `context_window`；`reasoning_effort` 必须在 `capabilities.reasoning_efforts` 中；不能禁用激活行。
- 启动时：
  1. `load_active_model()` 读取激活行；
  2. 把所有启用的行按 provider 分组，渲染成 `llm-pi-ai` 的 `providers` 覆盖配置，写到 `/var/lib/qqbot/dsh-state/models.patch.json`；profile 里的 `llm-pi-ai` 只挂载 adapter，`providers` 为空。内置 provider（`opencode` / `opencode-go`）只覆盖 `apiKeyEnv`、`baseURL` 和模型参数，**不写 `api`**——否则会替换掉 DSH 内置的 adapter 包装，丢掉 `x-opencode-session`；自定义 provider 仍写完整定义；
  3. `make_dsh_client()` 用激活行的 `provider` / `model_id` / `max_output_tokens` / `reasoning_effort` 构造 `DeepSeekHarness`，并通过 `patches=` 传入上面的 patch；
  4. 回退客户端（`[dsh] enabled = false`）同样从激活行取 `base_url`、key、`max_tokens`、`reasoning_effort`。
- 同一 provider 下的行必须共享 `api` / `base_url` / `api_key_env`，不一致时渲染报错。
- 管理命令：`--list-models`、`--use-model p/m`、`--set-model p/m field=value ...`；修改后重启生效。

## 角色卡库（`bot/cards.py`）

- SQLite `/var/lib/qqbot/characters.db`，表 `cards`，主键 `id`；部分唯一索引保证全表最多一行 `is_active = 1`。
- 入库时统一归一化为 V3 `payload`（JSON），并抽出 `name` / `nickname` / `description` / `personality` / `scenario` / `tags` / `creator` 用于展示和搜索；`fingerprint` 是 payload 的 SHA-256，用于去重。
- 别名单独存在 `aliases` 列（JSON 数组，最多 8 个、每个 2–32 字），不写进 payload，所以不影响 `fingerprint` 去重；`Bot.refresh_card()` 会把 `name` / `nickname` / 别名一起放进 `_card_names`，供「叫到名字」判定使用。
- 搜索用 `LIKE` 逐字段匹配（关键词之间 AND、字段之间 OR），不依赖 FTS5 分词器，中文子串可以直接搜；`%` / `_` 会被转义。
- 安装来源：`url`（`/card install <url>`、`--install-card`）、`image`（管理员发来的 PNG）、`file`（服务器路径）、`bootstrap`（旧版单卡文件）。下载上限 12 MB，只接受 PNG 或 JSON。
- `active_signature()` 返回 `(id, updated_at, fingerprint)`，每轮消息只查一次；变化时 `Bot.refresh_card()` 重建 system prompt，并调用 `DSHClient.set_system_prompt()` 重启 DSH runtime。命令行的 `--use-card` 由同一机制在下一轮消息被发现。
- `bootstrap` 兼容：库为空时导入 `[cards] bootstrap`；只要这张卡仍然激活，就按 mtime 重新导入，保留原来「改文件即热加载」的行为。
- 命令行：`--list-cards`、`--search-cards <query>`、`--install-card <url|path>`、`--use-card <id|name>`、`--show-card`、`--add-card-alias <name>`、`--remove-card-alias <name>`。

## 表情包库（`bot/stickers.py`）

- SQLite `/var/lib/qqbot/stickers.db` + 文件目录 `/var/lib/qqbot/stickers/`；一行一张图，`sha1` 唯一，按内容去重。
- 入站判定：OneBot `image` 段带 `summary="[动画表情]"` 或 `sub_type=1` 时视为表情包；解析器同时把它标成 `[表情包]` 写进文本，`MessageEvent.stickers` 单独带出 URL。
- 只收群聊，私聊不收；下载异步执行，不阻塞回复；每个群每小时有收藏上限（`[stickers] max_per_hour`），单张上限 `max_bytes`，只接受 PNG/JPEG/GIF/WebP（按魔数判断，不信任 Content-Type）。
- 管理：`/sticker list|search|show|add|import|delete|ban|unban|stats`；`import` 走 NapCat 的 `fetch_custom_face`，把账号里已收藏的表情拉进库。
- 收进来后用视觉模型打标：一次调用同时给描述和 1-3 个情绪标签，写进 `description` / `tags`；打标失败不影响入库。
- 发送：prompt 里带一句约定，模型想发表情时在回复最后写 `[表情: 语气词]`；adapter 用 `extract_sticker_marker` 摘掉标记，按标签/描述匹配（`StickerStore.pick`）挑一张，以 `base64://` 图片段跟在文字后面发出，并受每会话冷却/小时上限约束。
- 图片输入走 DSH 官方 content block（ACP 形状 `{"type":"image","data":<base64>,"mimeType":...}`），不再先转文字描述；只有 thin client 路径和打标用 `image_url`。
- 表里的 `status='banned'` 不会进入候选；`uses` / `last_used` 记录发送情况。

## 定时任务（`bot/tasks.py`）

- 仅管理员：两条创建路径——直接说话（模型在回复里输出 `[定时: 原始描述]`）或 `/task add <自然语言>`。模型只在发言者被标注为“管理员”时收到这句提示；adapter 在 `_handle_task_request` 里再次校验 `is_admin`，非管理员的标记会被静默剥掉、不创建任务。
- 语义化解析：一次 thin client 调用把描述转成 `TaskSpec`（`daily|weekly|once|interval` + 时间/星期/间隔 + 要发送的内容），Pydantic 校验字段组合与取值，`next_run_for()` 计算严格晚于当前时间的下一次执行。
- 存储：`/var/lib/qqbot/tasks.db` 的 `tasks` 表，记录目标会话（群/私聊）、原始描述、消息内容、排期字段、启用状态、下次/上次执行和次数；重启后从 `next_run` 继续。
- 执行：`TaskScheduler` 在事件循环里每 `[tasks] check_seconds` 秒查一次到期任务；先原子推进 `next_run`（一次性任务直接禁用）再发送，失败不会重复轰炸；发送复用 OneBot 文本接口。
- 自然语言管理：同一套标记协议还支持 `[定时列表]`、`[定时删除: 关键词]`、`[定时暂停: 关键词]`、`[定时恢复: 关键词]`；adapter 只在当前会话的任务里按 id/关键词匹配，非管理员直接剥掉标记。
- 文案：`mode=fixed` 原样发送 `message`；`mode=generate` 到点用当前角色卡的系统提示词让模型现写一句，失败回落到 `message`。
- 限制：`max_tasks` 总数、`max_per_chat` 每会话上限；`interval` 最小 5 分钟；消息最长 300 字；一次性任务迟到超过 12 小时直接丢弃而不是补发。

## 赛程输出（`bot/schedule.py`）

- 系统提示词要求模型把赛程问题写成 ```schedule 代码块，内容为 `{event, matches[{stage, start_time, team_a, team_b, best_of, note}]}`。
- `extract_schedule()` 从回复里取出代码块，用 Pydantic 校验：`start_time` 必须是可解析时间（无时区按 +08:00 处理），队名不能为空，`best_of` 限定 1–9；按时间排序。
- 校验通过后由 `render_schedule()` 渲染固定格式（北京时间、无来源链接），代码块本身不会出现在聊天里；校验失败的块会被剥掉并提示重试，不会泄露原始 JSON。

## 上下文与压缩

- 上下文长度来自模型注册表的 `context_window`（付费 Flash 1M，free 200k），经渲染后成为 DSH route 的 `contextWindow`。token meter 和 compaction 使用这个值；模型本身不需要被 prompt 告知。
- `compaction-basic` 在 step boundary 检查 token 压力，默认 `thresholdRatio = 0.8`；本项目显式设置为：

```yaml
thresholdRatio: 0.8
headroomTokens: 16384
retainRatio: 0.16
auto: true
```

- 有效阈值 = `min(0.8 × contextWindow, contextWindow − 输出预留 − headroomTokens)`；1M 窗口约 800k，200k 窗口约 160k。
- 超过阈值时，较旧历史被总结成摘要，保留最近约 16% 的上下文；完整 session 仍以 JSONL 持久化。
- 激活模型的 `max_output_tokens`（默认 2048）限制单次输出，也作为压缩时的输出预留。
- `DSH_CONTEXT_WINDOW` 只对直连 `llm-deepseek` adapter 有意义；自定义 `llm-pi-ai` route 不需要它。

## 文件记忆

- `memory/lore.md`：角色卡世界书，启动/换卡时由 adapter 生成；
- `memory/notes/`：模型自由写入的事件、约定、摘要；
- `memory/profiles/`：旧版 JSON 画像的兼容目录（SQLite 版不再写入，可删除）。

工作区是模型唯一可写的文件区域。密钥、数据库和 DSH session 日志都在工作区之外。

## 失败与恢复

- **OneBot 断开**：adapter 指数退避重连；DSH 不受影响。
- **DSH runtime 崩溃**：`DSHClient.run` 捕获异常、关闭旧 runtime，下一轮重新启动；session JSONL 保留。
- **模型 401/429/5xx**：DSH 把错误写进 `turn/end`，adapter 记录并在 `notify_errors = true` 时发一条简短提示。
- **进程重启**：systemd `Restart=on-failure`；QQ 登录态和 DSH session 都在磁盘上。
- **角色卡损坏**：加载失败时记录错误并回复通用错误消息，不中断其他会话。

## 版本与升级

- `deepseek-harness-sdk` 固定 `0.2.1rc2`；DSH 的插件 API 仍是 developer preview。
- 升级步骤：改 `deploy/install-bot.sh` 里的版本 → 重新安装 → 用 `dsh --profile qqbot --dump-config` 验证 profile → 跑 `--check-dsh`。
- 如果 DSH 改了 `agent/created`、`ctx.systemPrompt` 或 `ToolDefinition`，需要同步调整 `dsh/user-profile.mjs`。
