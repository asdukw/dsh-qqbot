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
- 解析消息段（text / at / image / face / record / poke 等），去重；
- 决定是否回复：私聊全回；群聊在被 @、叫当前角色名、引用机器人消息或被戳一戳时回复，其余消息按主动策略参与；第三方之间的戳一戳只记入群上下文；
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
- `session-history`（自定义插件，按需读取旧 session JSONL / chat_events）
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
7. 图片：进入 DSH 历史的内容只有文字。图片下载后按 SHA-1 存到 `<dsh.workspace>/images/<sha1>.<ext>`（`bot/images.py`），用当前模型生成一次描述并缓存到 `memory.db` 的 `image_captions` 表；prompt 里只放 `[图片附件] 描述 + 相对路径`，模型需要细节时用 `read_image` 工具按路径读取原图（`tool-fs.readImage = true`）。同一张图重复出现只调用一次视觉模型。`[dsh] image_mode = "inline"` 可回退到旧的图片块行为；thin client 路径仍然使用 `image_url`。
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

## 历史插件（`dsh/session-history.mjs`）

DSH 的上下文只包含当前 generation；旧 session JSONL 在 `$DSH_HOME/sessions` 下、sandbox 工作区之外，文件工具读不到。这个插件把它们变成三个只读、按需调用的工具：

- `history_sessions`：扫描 `$DSH_HOME/sessions/<workspace>/<session>/session.*.jsonl`，按当前会话前缀过滤（`group-<gid>` / `private-<uid>`，含 `-gN`），返回当前/旧 generation 的起止时间、轮数、大小；
- `history_read`：流式读取指定 session 的 `user/message` 和 `assistant/message` 事件，支持关键词、`since` / `until`（北京时间）、`limit` 和最新/最早窗口；输出保持对话顺序；
- `chat_search`：查 `memory.db.chat_events` 当前会话的入站消息，包含群里未 @ 机器人的普通消息；保留由 `[usage] event_retention_days` 控制（默认 90 天），不包含助手回复。
- 会话身份来自 `profile.db.conversations`（插件按 `exec.agent.id` 查，不解析 session id）；读不到身份时回退解析 `group-*` / `private-*`。跨会话的 session 名会被拒绝。
- 在 `agent/created` 里注册 section `session-history`（order 8100），告诉模型“先查再答、不要假装记得没读过的内容”。
- 插件只读；只有模型主动调用时才读，不会每轮注入上下文。

## 模型注册表（`bot/models.py`）

模型配置的唯一来源。代码和环境变量里都不再有模型 id、上下文长度、输出上限或推理强度。

- SQLite `/var/lib/qqbot/models.db` 分两张表：
  - `providers`：主键 `provider`，字段 `display_name`、`api`、`base_url`、`api_key`、`enabled`、`created_at`、`updated_at`；一个供应商的模型共享这些连接信息。`api_key` 是面板里直接保存的密钥（DB 文件 0600）。旧记录仍保留一个内部 `api_key_env` 兼容字段，界面不再暴露，新供应商默认不依赖它。
  - `models`：主键 `(provider, model_id)`，字段 `display_name`、`context_window`、`max_output_tokens`、`reasoning_effort`（默认空，表示模型默认值）、`capabilities`（JSON，含 `reasoning_efforts` / `input`）、`enabled`、`is_active`。
- `is_active` 上有部分唯一索引，保证全表最多一行激活；`use()` 在一个事务里切换。
- 空库时写入默认供应商 `opencode-go` 和模型 `opencode-go/deepseek-v4.1-flash`（1M 激活）；`meta.seeded` 保证只 seed 一次，之后不覆盖用户修改，删空模型也不会重新写入。
- 旧库迁移：`_migrate()` 补列、把旧的 `opencode/*` 默认行迁移到 `opencode-go/*`，并把每行重复的 `api` / `base_url` / `api_key_env` 拆分到 `providers`，然后重建 `models` 表（去掉旧的连接字段）。
- 写入校验：新建供应商必须有 `provider`（小写 slug）和 `base_url`；新建模型必须有 `context_window`；`reasoning_effort` 必须在 `capabilities.reasoning_efforts` 中；不能禁用/删除激活模型，也不能禁用拥有激活模型的供应商；供应商下还有模型时不能删除供应商。
- 启动时：
  1. `load_active_model()` 读取激活行；
  2. 遍历启用的供应商及其启用的模型，渲染成 `llm-pi-ai` 的 `providers` 覆盖配置，写到 `/var/lib/qqbot/dsh-state/models.patch.json`；profile 里的 `llm-pi-ai` 只挂载 adapter，`providers` 为空。若供应商保存了 API Key，渲染时把它放进按供应商生成的 `QQBOT_PK_<slug>_<hash>` 环境变量，patch 的 `apiKeyEnv` 引用该变量（密钥不落 patch 文件）；DSH runtime 作为 Python 子进程启动，继承这个环境变量，再由 credentials 层按请求解析。内置 provider（`opencode` / `opencode-go`）只覆盖 `apiKeyEnv`、`baseURL` 和模型参数，**不写 `api`**——否则会替换掉 DSH 内置的 adapter 包装，丢掉 `x-opencode-session`；自定义 provider 仍写完整定义；
  3. `make_dsh_client()` 用激活行的 `provider` / `model_id` / `max_output_tokens` / `reasoning_effort` 构造 `DeepSeekHarness`，并通过 `patches=` 传入上面的 patch；
  4. 回退客户端（`[dsh] enabled = false`）同样从激活行（JOIN 供应商）取 `base_url`、key、`max_tokens`、`reasoning_effort`。
- 管理入口：Admin UI 的「模型」页（`POST /api/models/*`，登录 + JSON + 同源）按供应商 → 模型两级管理，API Key 在表单里直接填写并存入 `providers.api_key`；CLI 保留 `--list-models`、`--use-model p/m`、`--set-model p/m field=value ...`。修改后机器人在下一轮消息自动热切换。

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
- 管理：`/sticker list|search|show|add|import|delete|ban|unban|stats`；`import` 走 NapCat 的 `fetch_custom_face`，把账号里已收藏的表情拉进库。Admin UI 的表情库页直接展示图片（见下文 Admin UI 结构）。
- 收进来后用视觉模型打标：一次调用同时给描述和 1-3 个情绪标签，写进 `description` / `tags`；打标失败不影响入库。
- 发送：prompt 里带一句约定，模型想发表情时在回复最后写 `[表情: 语气词]`；adapter 用 `extract_sticker_marker` 摘掉标记，按标签/描述匹配（`StickerStore.pick`）挑一张，以 `base64://` 图片段跟在文字后面发出，并受每会话冷却/小时上限约束。
- 图片输入默认走占位符（见“一条消息的路径”第 7 步）：原图存 workspace、描述缓存、prompt 只带描述和路径；`read_image` 按需读原图。`[dsh] image_mode = "inline"` 时才使用 DSH content block；thin client 路径和打标仍用 `image_url`。
- 表里的 `status='banned'` 不会进入候选；`uses` / `last_used` 记录发送情况。

## 订阅转发（`bot/feeds.py`）

- `feeds.db` 存 `(chat_type, chat_id, feed_url)` 唯一订阅；`/sub add` 支持 B 站 UID/空间链接（拼成 RSSHub 动态路由）或任意 RSS/Atom 地址，首次添加时把当前条目记入 `seen` 作为基线，避免补发历史。
- `FeedPoller` 每 `[feeds] check_seconds`（默认 300s）按 URL 去重抓取一次，RSS/Atom 由标准库解析；新条目按时间顺序转发，单次最多 `max_new` 条；失败写入 `last_error` 并在 `/sub list` 里显示。
- B 站对阿里云机房 IP 直接返回 `-412 request was banned`，因此默认 `rsshub_base = https://rsshub.liumingye.cn`（可换镜像）。

## 语音、梗图与记忆检索

- 语音：`bot/voice.py` 调 edge-tts 生成 mp3，`record` 段以 base64 发送（NapCat 实测接受 mp3）；模型用 `[语音]` 标记，超长或合成失败退回文字，每会话 30 秒冷却。
- 梗图：`bot/meme.py` 用 Pillow 把 `[梗图: 文字]` 画到一张收藏表情（或渐变底卡）上，再走图片消息发送；每会话 30 秒冷却。
- 记忆：`memory.db` 增加 external-content FTS5（trigram，中文子串可用）和增删触发器；回复前用当前文本的 3-gram 检索同会话历史，取前 3 条注入 `[相关历史]`，短词回退 LIKE。

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

## 会话轮换（`bot/sessions.py`）

- DSH session id 由 `conversation_id` 加 generation 组成：generation 0 保留历史 id（如 `group-123`），之后为 `group-123-gN`；旧 JSONL 不删除。
- 默认策略：群聊空闲 24 小时、私聊空闲 168 小时、群聊每天 04:00 后第一条消息轮换；可选 `session_rotate_max_hours` / `session_rotate_max_messages`。配置在 `[dsh]`。
- 启动时扫描现有 DSH JSONL，用最早/最晚事件时间和 `user/message` 数回填 `memory.db` 的 `session_state`，避免旧会话被误判为刚开始。
- `/reset`（管理员，`/new` 同义）强制新 generation；`memory.db` 的 FTS recall 按 `conversation_id` 保留，轮换不影响长期记忆。
- 压缩仍保持 80% 兜底；先靠轮换降低历史重放，等 token 账本上线后再决定是否把阈值降到约 20%。

## 关系 / 好感度（`bot/relations.py`）

- 每个 QQ 一行，存在 `profile.db` 的 `user_relations` 表（与 DSH 用户画像同库）：好感度 `0~100`（默认 20）、关系标签、私下印象、公开印象、最近活动/更新时间、每日变更计数。
- 主模型在回复末尾输出隐藏标记 `[好感+2]` / `[印象: …]` / `[关系: …]`；`Bot._send_reply` 在发送前剥离并写入 `RelationStore`。群聊里的 `[印象]` 写 `public_impression`，私聊里写 `impression`；增量夹在 `±max_delta`，有冷却和每日变更上限；衰减暂不启用。
- `user_facts` 带 `group_id` 作用域：群里学到的 fact 只在该群注入/查询，私聊只能看到全局 facts；旧数据迁移时按 `source=group:<gid>` 回填。`/relation` 的画像同样按当前群过滤。
- `/relation`（别名 `/关系`、`/好感度`、`/印象`）对所有人开放：默认查自己，`/relation <QQ>` 查他人；群聊只展示公开印象，私聊展示私下印象；其他人不展示印象，只看分数/等级和公开 facts。管理员可用 `/relation set|reset`。
- `dsh/user-profile.mjs` 通过 `{{user_relation}}` 把压缩后的关系块注入 DSH 系统提示（群聊用公开印象，私聊用私下印象）；thin 路径在 `_reply_llm` 的 system message 里追加同一块和标记说明。

## Token 审计与群聊统计

- `usage.db` 的 `usage_events` 统一记录非缓存输入、缓存输入和输出。DSH 侧由 `DSHClient.run_with_events()` 返回本轮 events，`UsageStore.record_dsh()` 解析 `assistant/message` 和 `compaction/summary` 的 usage；thin client 侧由 `LLMClient.usage_sink` 上报图片描述、表情打标、任务解析/文案等调用。
- 启动时 `UsageStore.backfill_dsh()` 从现有 DSH JSONL 回填历史 usage，按 session/seq 幂等去重；旧数据也能进入面板。
- `memory.db` 的 `chat_events` 记录每条群聊/私聊入站消息（发送者、文本、时间），供「今日发言排行」和「今日词云」使用；词云优先用 jieba，缺失时回退正则。
- Admin UI 的 `/api/usage` 支持 `days` 和 `conversation`（群/好友）筛选；`/api/fun` 支持 `conversation` 与 `start` / `end`（`YYYY-MM-DD`，北京时间，含首尾，缺省为今天），返回该范围的排行、词云和会话列表；聊天分析页提供「今天 / 近一周 / 近一月」快捷按钮和起止日日历。

## Admin UI 结构

- `bot/admin_ui.py` 只做 JSON API + 静态文件；React 端用 hash 路由（`#/tasks` 等）分组，服务端不需要额外的路由配置。
- 资源接口各自读一个 SQLite：`/api/models`、`/api/cards`、`/api/tasks`、`/api/feeds`、`/api/stickers`；`/api/stickers` 返回每张表情的元数据和 `has_image`（文件是否仍在库目录内），`/api/stickers/<id>/image` 流式返回原图，`?thumb=1` 用 Pillow 生成 320px 的 JPEG 缩略图并做进程内 LRU 缓存；两个接口都会校验文件路径位于 `[stickers] directory` 之下，越界或缺失一律 404；`/api/cards` 只返回列表摘要，`/api/cards/<id>` 额外解析出库里存的 V3 payload，供角色卡详情页按 V3 字段分栏展示（内嵌的 `data:` 资产只保留前缀）；`/api/conversations` 合并 `memory.db` 的 `chat_events` / `session_state`、`profile.db` 的 `conversations` 与 `usage.db` 的按会话汇总。
- `POST /api/models/provider-models` 用已保存的供应商 `base_url` 和 API key 拉取 OpenAI-compatible `/models`，供模型表单的「获取模型」按钮生成模型 ID 下拉列表；接口只返回 id / owned_by，不返回也不记录密钥。
- `/api/models` 通过 `ModelRegistry` 打开 `models.db`（会执行与机器人相同的迁移），返回 `providers`、`models`、`presets` 以及每个供应商的 `api_key_set` / `api_key_source`（`stored` / `env` / `missing`；不返回密钥本身）。模型页的写入走 `POST /api/models/providers`、`/api/models/providers/delete`、`/api/models/items`、`/api/models/items/delete`、`/api/models/active`；这些接口要求登录 cookie、`Content-Type: application/json` 和同源 `Origin`，并复用 `ModelRegistry` 的校验（不能禁用/删除激活模型、不能删除仍有模型的供应商等）。
- `/api/overview` 返回仪表盘所需的计数、今日指标、按自然日（Asia/Shanghai）的活动趋势、订阅异常和最近消息；`/api/system` 返回数据库路径/大小与面板配置。
- `/api/dashboard` 保留为旧版一次性聚合接口，内部复用上述资源函数；新页面不再使用它。
- 所有 `/api/*` 都需要 HMAC cookie；前端 401 时回到登录页，每 60 秒重拉当前页。

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
- 工具结果默认裁剪：官方 `spill-local` + `spill-policy` 已挂载（`maxInlineTokens: 3000`）。超过预算的工具结果会把完整文本写到 `/var/lib/qqbot/dsh-workspace/spill/`，模型只看到头尾预览和 notice 里的 `read`/`grep` 路径；`read` 自身不会再被 spill，避免循环。文件默认保留 14 天。
- 二级兜底：官方 `compaction-tool-result-pruner`（`thresholdChars: 8192` / `headChars: 4096` / `tailChars: 1024`）在 `compaction-basic` 压力压缩或 context overflow 时继续裁剪中间段；该过程不额外调用模型。
- 激活模型的 `max_output_tokens`（默认 2048）限制单次输出，也作为压缩时的输出预留。
- `DSH_CONTEXT_WINDOW` 只对直连 `llm-deepseek` adapter 有意义；自定义 `llm-pi-ai` route 不需要它。

## 文件记忆

- `memory/lore.md`：角色卡世界书，启动/换卡时由 adapter 生成；
- `memory/notes/`：模型自由写入的事件、约定、摘要；
- `memory/profiles/`：旧版 JSON 画像的兼容目录（SQLite 版不再写入，可删除）。

工作区是模型唯一可写的文件区域。密钥、数据库和 DSH session 日志都在工作区之外；旧 session JSONL 由 `session-history` 插件只读、按需访问。

## 失败与恢复

- **OneBot 断开**：adapter 指数退避重连；DSH 不受影响。
- **DSH runtime 崩溃**：`DSHClient.run` 捕获异常、关闭旧 runtime，下一轮重新启动；session JSONL 保留。
- **模型 401/429/5xx**：DSH 把错误写进 `turn/end`，adapter 记录并在 `notify_errors = true` 时发一条简短提示。
- **进程重启**：systemd `Restart=on-failure`；QQ 登录态和 DSH session 都在磁盘上。
- **角色卡损坏**：加载失败时记录错误并回复通用错误消息，不中断其他会话。

## 版本与升级

- `deepseek-harness-sdk` 固定 `0.2.1rc2`；DSH 的插件 API 仍是 developer preview。
- 升级步骤：改 `deploy/install-bot.sh` 里的版本 → 重新安装 → 用 `dsh --profile qqbot --dump-config` 验证 profile → 跑 `--check-dsh`。
- 如果 DSH 改了 `agent/created`、`ctx.systemPrompt` 或 `ToolDefinition`，需要同步调整 `dsh/user-profile.mjs` 和 `dsh/session-history.mjs`。
