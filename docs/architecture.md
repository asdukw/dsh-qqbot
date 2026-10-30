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
- 角色卡库（SQLite）与热切换、会话角色绑定、独立系统提示词和世界书注入；
- 长回复拆分、发送失败日志；
- 好友/群请求的接收、去重和审批执行（`approvals.py`）；
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
4. 读取当前 conversation 的人设快照，按 token/revision 轮换该会话 generation；每轮写入 `session_personas` 的专属 prompt，不再生成共享 `memory/lore.md` 或因切卡重启整个 DSH。
5. 把身份写进 `profile.db` 的 `conversations` 表：`session_id → scope, user_id, group_id, sender_name`（`bot/conversations.py`）。
6. 群聊时取出并清空该群缓冲，和被叫到的那句拼成一条用户消息：`[群聊里最近的消息] … [有人 @ 你] 昵称(QQ号): 内容`；每行都带发言者 QQ 号（群名片相同的人也能区分），消息头的发言者若还有不同的 QQ 昵称会额外注明。
7. 图片：进入 DSH 历史的内容只有文字。图片下载后按 SHA-1 存到 `<dsh.workspace>/images/<sha1>.<ext>`（`bot/images.py`），用当前模型生成一次描述并缓存到 `memory.db` 的 `image_captions` 表；prompt 里只放 `[图片附件] 描述 + 相对路径`，模型需要细节时用 `read_image` 工具按路径读取原图（`tool-fs.readImage = true`）。同一张图重复出现只调用一次视觉模型。图片准备是并行的，每张最多 `[dsh] image_prepare_timeout`（默认 30s）；描述调用 `image_caption_timeout`（默认 15s）且不重试，超时就退化为文字 + 原图路径，不会让回复等上几分钟。`[dsh] image_mode = "inline"` 可回退到旧的图片块行为；thin client 路径仍然使用 `image_url`。
8. `asyncio.to_thread(dsh.run, session_id, prompt)`：
   - DSH 启动/复用 runtime；
   - 加载或创建 `session_id` 的 JSONL 历史；
   - 组装 system prompt（插件按 `conversations` 查身份，注入用户画像和群画像）；
   - 跑 agent loop；
   - 如果模型调用画像工具或文件工具，执行并把结果回灌；
   - 返回 `final_response`。
9. adapter 按 `max_reply_chars` 拆分并发送。
10. 用户消息和助手回复同时写入本地 SQLite `messages` 表，仅作审计。
11. DSH 回复处理结束记录 `rid=... reply processing finished: conv=... queue=... prep=... dsh=... total=...`，直连 LLM 和主动回复也有相应结束日志；这不是全部内容已送达的证明，发送失败另记 ERROR。在锁/信号量上等待超过 3 秒会额外打一条 warning，便于定位排队、图片准备或 DSH 生成造成的延迟。

## 自主互动工具与可信目标快照

`dsh/interaction-tools.mjs` 注册 `reply_to_others(message_id,text,user_ids?)`、`mention_others(user_ids,text)`、`react_to_others(message_id,emoji_id)`。工具由 agent 按语境自主选择，不继承角色卡管理员门槛或视频发送的逐次明确请求门槛。群聊在原有正常/主动回复策略入场后可用三者；私聊仅引用/reaction；定时、日报、戳一戳等合成轮次不获得 lease，DSH 关闭时不额外引入 tool-call loop。

`GroupLine` 保留真实 `user_id/message_id/reply_to`，adapter 为每轮建立不可变、经预算裁剪的可见快照。prompt、图片和可操作集合都从该快照派生，裁掉的消息不能成为隐藏目标；引用关系本身不授权未展示的原消息，数据库审计行号不是 QQ message_id。规范十进制字符串避免 JS 精度损失；非零有符号消息 ID 可用于引用，现有 reaction helper 对负 ID 返回不支持。主动轮次在确认/可能互动后只消费已捕获的上下文，保留模型执行期间新到的消息。

`InteractionService` 在随机 loopback 端口发放绑定 session、固定 conversation、可信快照、截止时间与预算的独立 lease。插件仅在 `QQBOT_INTERACTION_BRIDGE/SECRET/LEASE` 有效时注册 schema 与 agent-scoped 提示词；`node:http` 直连 `/interaction`，动作请求 JSON 为 `{lease,session,args,operation,receipt_ack:true}`，Bearer secret 验证，不走代理或重定向，请求/响应及取消超时均有界。模型不能指定收件会话；bridge 重验额外字段、目标、时限、正文内容策略/隐藏标记及预算，禁止任意收件人和 `@all`。每轮至多 6 个不同动作、3 条文字，每条至多 @ 3 人；正文受 `max_reply_chars` 与硬上限 4000 约束，超长明确失败。

动作在首次外部 I/O 前认领预算并记录规范化指纹，重复/并发调用复用结果而不重复发送。OneBot 窄 helper 组装独立 `reply/at/text` 消息段，CQ 字符串不能借正文变成控制动作。文字需有效发送回执才记 `sent` 并登记 own-message/proactive 状态；reaction 经现有 `set_msg_emoji_like`（2 秒）确认才记 `reacted`，不虚构新消息。超时、取消、断连或缺失回执记 `unknown`，冻结该轮后续外部动作，不自动重试；撤销或到期不能抹掉已发生的发送事实。

`DSHClient` 对普通 runtime 显式清空互动 env，授权轮次单独创建/关闭 runtime，保留 session 历史且不修改全局环境。正常轮次按各自权限合并 interaction/card/video env；主动轮次只新增 interaction 权限。取消先撤销 lease，再等待 runtime 退出，桥接关闭先于 DSH 关闭。收尾以服务端账本为准：unknown 不发 final；模型失败保留真实错误及已确认动作，不补发错误文字；已确认工具文字跳过 final 和其隐藏标记；仅 reaction 可带正常 final 或空 final 静默完成；没有动作仍按原有非空回复/错误策略处理。已发送工具文字只记忆一次，reaction 不伪装成聊天正文；deferred card switch 仍需正常结束且所有文字严格确认、无 unknown，仅 reaction 不满足门槛。


互动执行期限在 DSH 取得实际 runtime 锁后才启动，排队时间单独有界；取消中的记忆、session 及主动配额收尾由 shield 保护到完成。插件在本地串行分发后才启动单请求 deadline，避免并发工具请求在服务端队列中提前超时。已确认结果通过内部 receipt ACK 清除待确认投递标记；若原结果丢失，服务端保留不确定门槛，SDK 的真实 `tool/result` unknown 也会抑制 final，不解析模型自述。已验证 sent/reacted 后仅 ACK 响应丢失不推翻动作结果；ACK 根本未到则服务端保守抑制 final，不重试原动作。互动 unknown 同时撤销该轮其他 bridge 权限。

安装/profile 都注册 `/opt/qqbot/dsh/plugins/interaction-tools.mjs`。`scripts/check_interaction_tools_runtime.py` 在临时目录运行固定 SDK `0.2.1rc2`，本地 SSE 假模型检查实际 schema 并驱动三个工具，经真实 bridge 与 OneBot helper 到 mock API，验证多人引用/@、主动/私聊边界、仅 reaction 空终态及后续权限清空，不连接外部模型或 QQ。真实 SDK 的 no-block 终态会触发 `EMPTY_RESPONSE` 和内部模型重试，smoke 明确断言错误保留且 QQ reaction 只执行一次；thinking-only/空白文字终态另行断言正常完成且 visible final 为空，不把前者伪装成成功。QQ/NapCat 是否在客户端展示 reaction 仍需后续明确授权的真实客户端验收；已有视频链接自动 reaction 不因此改变。

## 身份传递

DSH SDK 的 `session/prompt` 只接受 `sessionId` 和 `contentBlocks`，没有元数据字段，插件拿到的只有 `agent.id`（即 session id）。因此身份走旁路：adapter 在每轮调用前写 `conversations` 表，插件在渲染 system prompt 和执行工具时按 session id 查询。插件不解析 session id 的格式；查不到身份时画像工具直接报错，system prompt 里画像显示为空。

两边共用同一个 SQLite 文件，WAL 模式加 `busy_timeout`，adapter 写入发生在 DSH 读取之前，没有并发写同一行的情况。

## 画像插件（`dsh/user-profile.mjs`）

- 在 `agent/created` 里为每个 agent 注册 **agent-scoped** 的变量 `user_profile`、`group_profile` 和 section `user-profile`（order 8000），只影响当前 agent。
- 用户画像 `user_facts`，主键 `(user_id, group_id, key)`；私聊 group_id 为空，群 facts 按当前群隔离：
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
  group_id   TEXT NOT NULL DEFAULT '',
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,
  visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
  source     TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, group_id, key)
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
  - `models`：主键 `(provider, model_id)`，字段 `display_name`、`context_window`、`max_output_tokens`（默认 0，表示不发送 `max_tokens`）、`reasoning_effort`（默认空，表示模型默认值）、`capabilities`（JSON，含 `reasoning_efforts` / `input`）、`enabled`、`is_active`。
- `is_active` 上有部分唯一索引，保证全表最多一行激活；`use()` 在一个事务里切换。
- 空库时写入默认供应商 `opencode-go` 和模型 `opencode-go/deepseek-v4.1-flash`（1M 激活）；`meta.seeded` 保证只 seed 一次，之后不覆盖用户修改，删空模型也不会重新写入。
- 旧库迁移：`_migrate()` 补列、把旧的 `opencode/*` 默认行迁移到 `opencode-go/*`，并把每行重复的 `api` / `base_url` / `api_key_env` 拆分到 `providers`，然后重建 `models` 表（去掉旧的连接字段）。
- 写入校验：新建供应商必须有 `provider`（小写 slug）和 `base_url`；新建模型必须有 `context_window`；`reasoning_effort` 必须在 `capabilities.reasoning_efforts` 中；不能禁用/删除激活模型，也不能禁用拥有激活模型的供应商；供应商下还有模型时不能删除供应商。
- 启动时：
  1. `load_active_model()` 读取激活行；
  2. 遍历启用的供应商及其启用的模型，渲染成 `llm-pi-ai` 的 `providers` 覆盖配置，写到 `/var/lib/qqbot/dsh-state/models.patch.json`；profile 里的 `llm-pi-ai` 只挂载 adapter，`providers` 为空。若供应商保存了 API Key，渲染时把它放进按供应商生成的 `QQBOT_PK_<slug>_<hash>` 环境变量，patch 的 `apiKeyEnv` 引用该变量（密钥不落 patch 文件）；DSH runtime 作为 Python 子进程启动，继承这个环境变量，再由 credentials 层按请求解析。内置 provider（`opencode` / `opencode-go`）只覆盖 `apiKeyEnv`、`baseURL` 和模型参数，**不写 `api`**——否则会替换掉 DSH 内置的 adapter 包装，丢掉 `x-opencode-session`；自定义 provider 仍写完整定义；
  3. `make_dsh_client()` 用激活行的 `provider` / `model_id` / `max_output_tokens`（0 时传 `None`，SDK 会省略该参数）/ `reasoning_effort` 构造 `DeepSeekHarness`，并通过 `patches=` 传入上面的 patch；
  4. 回退客户端（`[dsh] enabled = false`）同样从激活行（JOIN 供应商）取 `base_url`、key、`max_tokens`、`reasoning_effort`。
- 管理入口：Admin UI 的「模型」页（`POST /api/models/*`，登录 + JSON + 同源）按供应商 → 模型两级管理，API Key 在表单里直接填写并存入 `providers.api_key`；CLI 保留 `--list-models`、`--use-model p/m`、`--set-model p/m field=value ...`。修改后机器人在下一轮消息自动热切换。

## 角色卡库（`bot/cards.py`）

- SQLite `/var/lib/qqbot/characters.db`，表 `cards`，主键 `id`；部分唯一索引保证全表最多一行 `is_active = 1`。
- 入库时统一归一化为 V3 `payload`（JSON），并抽出 `name` / `nickname` / `description` / `personality` / `scenario` / `tags` / `creator` 用于展示和搜索；`fingerprint` 是 payload 的 SHA-256，用于去重。
- 别名单独存在 `aliases` 列（JSON 数组，最多 8 个、每个 2–32 字），不写进 payload，所以不影响 `fingerprint` 去重；名字寻址按当前会话的角色读取 `name` / `nickname` / 别名；别名本身仍是卡库共享元数据。
- 搜索用 `LIKE` 逐字段匹配（关键词之间 AND、字段之间 OR），不依赖 FTS5 分词器，中文子串可以直接搜；`%` / `_` 会被转义。
- 安装来源：`url`（`/card install <url>`、`--install-card`）、`image`（管理员发来的 PNG）、`file`（服务器路径）、`bootstrap`（旧版单卡文件）。下载上限 12 MB，只接受 PNG 或 JSON。
- `is_active` 只表示默认/回退卡。`bot/personas.py` 在同库保存 `persona_bindings`（会话、不可变 payload、epoch、memory cutoff）和 `persona_switches`（发送阶段、回执、错误）；`/card use` 不再修改全局默认。
- `profile.db.session_personas` 由 Python 在每次 DSH 调用前准备；`dsh/persona.mjs` 为 agent 注册动态、禁止二次插值的专属 prompt。世界书由该卡 `lorebook.select()` 注入，不落共享文件，局部切换不重启 runtime。
- `bot/persona_runtime.py` 持有每会话 guard 执行预告→绑定/轮换→opening，普通/主动回复、定时与日报 compose+send 共用 guard。切换队列由在线 OneBot 连接处理；发送超时为 unknown，不盲目重播。
- 切换轮换 session generation，并使用消息 ID cutoff 隔离 fallback history 和自动 FTS/LIKE recall；旧档案保留，显式查询旧 session 时提示此前人设阶段。
- `bootstrap` 兼容：库为空时导入 `[cards] bootstrap`；未引用的默认 bootstrap 卡按 mtime 重新导入；绑定/任务引用的版本不可原地改写或去重删除。
- 命令行：`--list-cards`、`--search-cards <query>`、`--install-card <url|path>`、`--use-card <id|name> --conversation group:<id>`、`--switch-status <id>`、`--resume-switch <id>`、`--show-card`、`--add-card-alias <name>`、`--remove-card-alias <name>`。

### 管理员语义角色工具

`dsh/card-tools.mjs` 注册 `card_library`、`card_online_search`、`card_install`，复用官方 `ctx.web.search` 与现有 keyless provider。模型只提供查询或候选 ID；插件将真实搜索 sources 直接交给 `bot/card_service.py` 的内部登记接口。Python 抓取静态页面真实下载 href/直接卡 JSON，不运行页面脚本、不猜站点 API；候选绑定 `(actor, conversation, batch, number, TTL)`，后续编号必须对应最新有效批次。

CardService 在随机 `127.0.0.1` 端口提供有凭证、有限请求体/并发/时间的桥接。真实管理员 chat 回合在 `_reply_dsh` 开始 DSH 调用时创建 lease，绑定原事件、session、persona token 和随机 nonce。固定 SDK 无逐轮 env/metadata，因此 `DSHClient.run_authorized` 在线程锁内关闭旧 runtime、以显式 SDK env 创建当轮独立 runtime，结束即关闭；普通 runtime 的 card env 为空，工具不注册。session ID/历史不变。取消先撤销 lease，再等旧线程关闭，防止迟到工具借用下一轮权限。代价是管理员聊天轮的额外启动开销。

`card_fetch.fetch_public_card_content` 是独立的自主下载边界：公网 HTTPS/443、禁代理、固定已校验 IP、原始 Host/TLS SNI、逐跳验证、总超时、12 MiB 上限、拒绝 HTTP 压缩。`CharacterCard.from_bytes(strict=True)` 在归一化前验证原始卡结构、JSON 深度和 PNG 完整性，元数据解压有界；旧的显式管理员文件/附件导入保留兼容性。安装复用 `CardRegistry` 的归一化与指纹去重，不默认 use。

`card_library use` 只记录 `DeferredCardSwitch`，不在模型调用内 `switch_card` 或 `enqueue`：前者重入会话锁，后者会让当前 `_persona` 因 pending 拒绝发送。当前轮严格确认文本分片回执，返回 deferred，`handle` 退出 guard 后重新取会话锁同步复核 token/权限再入队，由 persona worker 完成预告/绑定/开场白。失败、取消或未知回执不入队，安装已经落库则仍保留。

验证入口：`tests/test_card_service.py`、`tests/test_card_fetch.py`、`tests/test_personas.py` 和 `scripts/check_card_tools_runtime.py`。SDK smoke 使用真实工具注册/搜索 seam/HTTP bridge/临时库，不依赖外网；公网来源可用性需另外只读检查，不能由 fixture 代替。

## 表情包库（`bot/stickers.py`）

- SQLite `/var/lib/qqbot/stickers.db` + 文件目录 `/var/lib/qqbot/stickers/`；一行一张图，`sha1` 唯一，按内容去重。入库时再计算 64-bit dHash（首帧、灰度、透明区域铺白），与已有 hash 的 Hamming 距离 ≤ `[stickers] phash_distance`（默认 4）视为同一张并复用已有记录；首次入库会回填旧记录的 phash。
- 入站判定：OneBot `image` 段带 `summary="[动画表情]"` 或 `sub_type=1` 时视为表情包；解析器同时把它标成 `[表情包]` 写进文本，`MessageEvent.stickers` 单独带出 URL。
- 只收群聊，私聊不收；下载异步执行，不阻塞回复；每个群每小时有收藏上限（`[stickers] max_per_hour`），单张上限 `max_bytes`，只接受 PNG/JPEG/GIF/WebP（按魔数判断，不信任 Content-Type）。
- 管理：`/sticker list|search|show|add|import|delete|ban|unban|stats`；`import` 走 NapCat 的 `fetch_custom_face`，把账号里已收藏的表情拉进库。Admin UI 的表情库页直接展示图片，详情弹窗可删除并选择是否屏蔽相似图片（见下文 Admin UI 结构）。
- 屏蔽：`sticker_blocks` 表保存被删除图片的 64-bit dHash。`StickerStore.add_bytes()` 在入库前检查新图与屏蔽哈希的 Hamming 距离，≤ `phash_distance` 直接抛 `StickerBlocked`；删除时 `delete_blocking()` 会一并删除库中所有相似行和文件。`/sticker delete <id> block` 与面板的删除按钮共用这条路径。
- 收进来后用视觉模型打标：一次调用同时给描述和 1-3 个情绪标签，写进 `description` / `tags`；打标失败不影响入库。
- 发送：prompt 里带一句约定，模型想发表情时在回复最后写 `[表情: 语气词]`；adapter 用 `extract_sticker_marker` 摘掉标记，按标签/描述匹配（`StickerStore.pick`）挑一张，以 `base64://` 图片段跟在文字后面发出，并受每会话冷却/小时上限约束。
- 图片输入默认走占位符（见“一条消息的路径”第 7 步）：原图存 workspace、描述缓存、prompt 只带描述和路径；`read_image` 按需读原图。`[dsh] image_mode = "inline"` 时才使用 DSH content block；thin client 路径和打标仍用 `image_url`。
- 表里的 `status='banned'` 不会进入候选；`uses` / `last_used` 记录发送情况。

## 订阅转发（`bot/feeds.py`）

- `feeds.db` 存 `(chat_type, chat_id, feed_url)` 唯一订阅；`/sub add` 支持 B 站 UID/空间链接（拼成 RSSHub 动态路由）或任意 RSS/Atom 地址，首次添加时把当前条目记入 `seen` 作为基线，避免补发历史。
- `FeedPoller` 每 `[feeds] check_seconds`（默认 300s）按 URL 去重抓取一次，RSS/Atom 由标准库解析；新条目按时间顺序转发，单次最多 `max_new` 条；失败写入 `last_error` 并在 `/sub list` 里显示。抓取对 `408/425/429/500/502/503/504` 和网络异常做有界退避重试（轮询额外 1 次、`/sub add` 额外 2 次，间隔 1 秒、3 秒），`FeedError` 带上 HTTP 状态码和 RSSHub 错误页的诊断摘要供日志排查；添加、重复添加和抓取/解析失败均有日志，URL 凭据与查询参数在写日志前脱敏。
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

## 好友/群请求审批（`bot/approvals.py`）

- NapCat 通过 OneBot 11 推送 `post_type=request`：好友请求 `request_type=friend`；群邀请 `request_type=group, sub_type=invite`；入群申请 `request_type=group, sub_type=add`。`MessageParser.parse_request()` 统一转成 `RequestEvent`，adapter 写入 `approvals.db`，按 `(kind, flag)` 唯一去重。
- 管理面板「审批」页读取 `approval_requests` 表，`POST /api/requests/action` 只把决定写成 `queued`（登录 + JSON + 同源校验，与模型写入接口一致）。
- `ApprovalWorker` 在 bot 进程里每 `[approvals] check_seconds` 抢一次 `queued` 行（`BEGIN IMMEDIATE` + `executing`），通过 bot 已有的 OneBot WS 执行 `set_friend_add_request` 或 `set_group_add_request`（群邀请带 `sub_type=invite`，入群申请带 `sub_type=add`），然后写回 `approved` / `rejected` / `failed`。Admin UI 不需要 NapCat 的 HTTP 端口或 token。
- 失败可重试；bot 重启时 `reset_executing()` 把中断的 `executing` 行重新排队。表里保留 `flag`、申请人/群、验证消息、时间、错误，供面板展示和排障。

## 每日日报（`bot/daily.py`）

- 定时器每 `[daily] check_seconds`（默认 60s）调用 `report_day()`：以 `hour:minute`（默认 00:00）为边界，返回“上一个完整自然日”；超过 `max_late_hours` 不再补发。因此 00:00 的含义是“在一天结束时总结昨天”。
- 会话与消息来自 `memory.db.chat_events`（只读连接），bot 自己的回复数来自同库的 `messages`；不在线时的日报不会丢：`daily.db` 的 `daily_reports` 按 `(conversation_id, day)` 记录 `sent` / `skipped` / `failed` 和尝试次数，重启后在迟到窗口内继续。
- 先算确定性统计：消息数/人数、排行、首末条、热词（复用 `admin_insights.top_words`）、复读连刷、按会话+日期固定种子的金句与宜忌。少于 `min_messages` 的会话记为 `skipped`。
- 锐评只调用一次 thin client（`purpose=daily_summary`，计入 `usage.db`），system prompt 用当前角色卡；聊天节选明确标注“只作素材”。生成失败、为空或超时都回落到固定文案，统计块照常发送。
- 发送走 `Bot._send_daily` → `_send_to_chat`，按 `max_reply_chars` 拆条；不写 DSH session，也不写 `messages` 历史。管理员的 `/daily [YYYY-MM-DD]` 直接调用 `DailyReporter.send_now()`，忽略 `min_messages`，用于预览或补发。

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
- `memory.db` 的 `chat_events` 记录每条群聊/私聊入站消息（发送者、文本、时间），供「今日发言排行」和「今日词云」使用。词云按常用关键词抽取流程：`jieba.posseg` 词性过滤（只保留 n/v/a/i/j/l/eng），丢弃 r/c/p/d/m/q/u/y/e/o/w 等；再排除成员昵称、停用词、语气词和两字母英文噪声。`count` 是消息覆盖数（同一条消息里重复的词只算一次），长区间用 `ROW_NUMBER()` 均匀抽样最多 2000 条，样本 ≥20 时默认过滤只出现一次的词。未安装 jieba 时回退到正则分词 + 停用词。
- Admin UI 的 `/api/usage` 支持 `days` 和 `conversation`（群/好友）筛选；`/api/fun` 支持 `conversation` 与 `start` / `end`（`YYYY-MM-DD`，北京时间，含首尾，缺省为今天），返回该范围的排行、词云和会话列表；聊天分析页提供「今天 / 近一周 / 近一月」快捷按钮和起止日日历。

## Admin UI 结构

- `bot/admin_ui.py` 只做 JSON API + 静态文件；React 端用 hash 路由（`#/tasks` 等）分组，服务端不需要额外的路由配置。
- 资源接口各自读一个 SQLite：`/api/models`、`/api/cards`、`/api/tasks`、`/api/feeds`、`/api/stickers`；`/api/stickers` 返回每张表情的元数据和 `has_image`（文件是否仍在库目录内），`/api/stickers/<id>/image` 流式返回原图，`?thumb=1` 用 Pillow 生成 320px 的 JPEG 缩略图并做进程内 LRU 缓存；两个接口都会校验文件路径位于 `[stickers] directory` 之下，越界或缺失一律 404；`POST /api/stickers/delete` 删除一张表情，`block_similar` 为真时同时删除相似行并把 dHash 写入屏蔽表；`/api/cards` 只返回列表摘要，`/api/cards/<id>` 额外解析出库里存的 V3 payload，供角色卡详情页按 V3 字段分栏展示（内嵌的 `data:` 资产只保留前缀）；仪表盘的会话计数仍由 `conversations_data` 合并 `memory.db` 的 `chat_events` / `session_state`、`profile.db` 的 `conversations` 与 `usage.db` 的按会话汇总，但不再暴露独立的会话接口和页面。
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
- 激活模型的 `max_output_tokens` 默认为 0：不发送 `max_tokens`，由提供商/模型决定单次输出上限；非 0 时限制单次输出并作为压缩时的输出预留。
- `DSH_CONTEXT_WINDOW` 只对直连 `llm-deepseek` adapter 有意义；自定义 `llm-pi-ai` route 不需要它。

## 文件记忆

- 角色卡和世界书按 session 注入，不再写共享 `memory/lore.md`；
- `memory/notes/`：共享工具工作区，不承担个人剧情/私密关系存档；
- `memory/profiles/`：旧版 JSON 画像的兼容目录（SQLite 版不再写入，可删除）。

工作区是模型唯一可写的文件区域。密钥、数据库和 DSH session 日志都在工作区之外；旧 session JSONL 由 `session-history` 插件只读、按需访问。

## 失败与恢复

日常操作步骤（日志、备份与恢复、重启、常见故障）见 [运维与排障](ops.md)。

- **OneBot 断开**：adapter 指数退避重连；DSH 不受影响。
- **DSH runtime 崩溃**：`DSHClient.run` 捕获异常、关闭旧 runtime，下一轮重新启动；session JSONL 保留。
- **模型 401/429/5xx**：DSH 把错误写进 `turn/end`，adapter 记录并在 `notify_errors = true` 时发一条简短提示。
- **进程重启**：systemd `Restart=on-failure`；QQ 登录态和 DSH session 都在磁盘上。
- **角色卡损坏**：加载失败时记录错误并回复通用错误消息，不中断其他会话。

## 版本与升级

- `deepseek-harness-sdk` 固定 `0.2.1rc2`；DSH 的插件 API 仍是 developer preview。
- 升级步骤：改 `deploy/install-bot.sh` 里的版本 → 重新安装 → 用 `dsh --profile qqbot --dump-config` 验证 profile → 跑 `--check-dsh`。
- 如果 DSH 改了 `agent/created`、`ctx.systemPrompt` 或 `ToolDefinition`，需要同步调整 `dsh/user-profile.mjs` 和 `dsh/session-history.mjs`。

## Galgame 运行时

`story_models.py` 定义有界、无环的章节图、条件选项、效果与延迟事件；Pydantic 拒绝未知字段，规则验证引用、可达性、保底选项和关系前置。世界包含人物摘要与分阶段语气；不是完整剧情树，也不执行卡片脚本。

`story_generation.py` 只用无工具 compatible client，输入当前角色/世界、本人关系与已确认事件，无群 DSH history 或共享文件记忆。`LLMClient.complete()` opt-in 返回 finish_reason/usage；默认 chat payload 不变。JSON 模式可配置，本地 schema/语义校验始终开启，最多一次修复，总超时/并发/长度有界。

`story_store.py` 在 profile.db 保存 schema 版本、world/run/relation/menu/action/event/outbox、权限、设置、审计及30天无正文生成诊断。关系身份为 conversation+character+user，周目独立 run_id；character_id 是本地稳定角色身份，卡片 fingerprint 与 persona epoch 不是存档键。

选项先验证本人/作用域/菜单版本/token/过期与去重，冻结效果在候选状态执行，生成下一幕且通过验证后，一次 BEGIN IMMEDIATE 事务消费菜单、写实际关系/状态、事件及 outbox。生成期间不占群会话锁或 SQL 事务，提交与发送短暂获取 conversation guard 并再次校验版本。网络 unknown 冻结、不自动重播；效果最多应用一次，不声称消息 exactly-once。

切卡跨 characters.db/profile.db 沿现有 saga：准备并保存世界引用和原始投递内容→绑定→公共序幕。未完成切换阻止普通新回复。A→B→A 读取旧档，继续时新签菜单 token，旧菜单失效但选项含义不变。restart 确认后归档本人目标关系/周目，forget 清除该目标活动与归档内容；真实 facts 不改。

普通聊天由 Python 准备 scoped relation_prompt，JS 插件只动态读取，不对新版关系另行分档/定价；旧 marker 仅兼容剥离。日常提案与剧情效果通过同一关系档，正向日预算不吞负向/印象。后台 mutation 认证/同源检查后入队，bot 重新核对完整目标和 revision；超过4096字节拒绝，不截断保存。

### 生成协议 v2：检查点与分层校验

`WorldBrief` 与 `StoryGraphDraft` 分别生成，本地组合成兼容的 `StoryDefinition`。检查点键包含 conversation/稳定角色/卡fingerprint/偏好hash/协议版本，未完成草稿不写入权威 world。只有完整验证通过后才执行原切卡bind；旧周目和既有world不重写。每阶段初次调用最多一次schema/语法反馈修复，候选正文只作为模型修复上下文，不进入公开日志；诊断只保留有界路径/类型/固定消息，不序列化input/ctx。截断、拒绝、内容策略、传输失败与校验失败分别报告。

发布硬约束保留ID/引用/flag/效果/安全保底/无环/可到达结局；孤立未引用节点、采样未覆盖等为质量警告，采样预算耗尽记unknown，不证明剧本无效。`story_warnings` 在真实初值与阈值边界上有界探索。原满属性单一模拟移除。显式refresh可签发基于当前关系的新菜单，保持原choice内容与效果，旧菜单失效；普通continue仍复用冻结选项。

排队、单次、阶段与准备总deadline分开，剩余时间约束每一次请求，传输attempts=1避免指数重试。场景不继承整图token上限。native严格输出仅显式配置；仅已知behavior闭域字典转换为固定五阶段对象，其他不支持schema拒绝，应用约束始终本地复验。面板展示attempt/stage/错误码和草稿状态，不暴露正文。

## 统一视频解析

`dsh/video-tools.mjs` 的 `video_resolve(source, part?)` 使用 Node 原生 HTTP 直连 loopback bridge，不使用环境代理或重定向。`VideoService` 在 `bot/main.py::_reply_dsh()` 为真实用户当前消息发放 session/actor/conversation 绑定的限时 lease；管理员轮次合并独立的 card/video 环境，普通用户不获得 card authority。定时、主动、合成事件不发 lease；`DSHClient` 清空未授权运行时的 video 环境。授权 runtime 在轮次前后关闭，session 数据保留；这以启动延迟换取简单、明确的权限边界。

`video_platforms.py` 规范化单视频标识/短链/分 P，`video_resolver.py` 定义版本化输出；`video_backend.py` 管理并发、限时、输出上限和元数据 LRU；`video_worker.py` 隔离固定版提取器，按平台显式选择 extractor，不自动委派 Generic、合集或内置媒体下载器。B站在固定提取器初始JSON hook里校验BV/AV、分P及CID；不再强制调用view API，pages来自已验证页面，匹配CID且非预览的内嵌DASH可复用。`video_http.py` 是所有后端元数据请求唯一的传输层：固定 `RequestDirector` 不允许其他 handler 兜底，逐跳检查平台域名和公共 DNS，实际连接 pin IP、Host/SNI 保持源站；原始/解压响应大小、累计流量和请求数有界。该集成边界不是防任意恶意 Python 代码的 OS 沙箱。

缓存只含投影后的元数据/资源描述，键含平台、来源/规范ID、显式分P及凭据generation（legacy为路径/mtime/size）。短链成功结果同时缓存规范目标；平台block/限流仅在独立冷却表保存60秒安全错误，普通错误不缓存。相同key用独立owner task共享只读结果，最多8个active key/32等待者，各等待者独立shield/deadline；最后等待者离开才取消owner，shutdown取消回收全部活动worker。出队后检查平台+generation冷却，materialize也使用同一gate但不共享文件/QQ任务。更新凭据后不把旧结果发布入新缓存。

worker 以最小环境启动，不继承 bot 模型密钥、代理或 bridge secret；不加载用户插件/配置/浏览器 Cookie，平台 Cookie 只在内存使用且不写回。未查询的字幕为 unknown，禁用可能主动下载字幕正文的 extractor 路径。输出最多 12 个格式摘要，不输出 Cookie、Header、签名媒体 URL 或原始对象。

单轮撤销/到期、HTTP 断开、解析超时及服务关闭会取消活动解析，terminate/kill/wait 回收进程；stdout/stderr 在读取时限制为 16 KiB/4 KiB。先停止 bridge/worker，再在线程中等待 DSH close 的锁，避免 asyncio loop 与 bridge 互等。静态配额为最多 24 次请求、5 次重定向、单响应 2 MiB、累计解码 8 MiB、工具响应 16 KiB，配置并发/总时限和缓存还有硬上限。

### 受限视频发送

`video_send` 通过同一 bridge 的 `operation=send` 调用 `video_sending.py`。`VideoLease` 附来源 message_id、原会话、服务器从当前未引用文本判定的单一来源及 DSH deadline；普通只读 lease 不自动变成发送授权。请求解析/下载/发视频时使用 send，讨论/总结仍 resolve。此保守规则不声称能完整识别任意自然语言；含糊或没有本轮链接时需重新确认附链接。

`video_media.py` 在 worker 内用独立 CDN allowlist 与流式 pinned-IP transport 下载一或两条轨道；不使用 yt-dlp downloader、不向 CDN 发平台 Cookie。`video_delivery.py` 创建私有 staging job、磁盘预留、校验固定 artifact manifest；主进程直接管理 ffmpeg/ffprobe，强制本地 MOV/MP4 demuxer、file-only protocol、显式映射、stream-copy、faststart，限制输出体积并核验时长/编码。验证完成后原子发布到只读映射给 NapCat 的 outbox，模型从不拿到本地路径、Header 或签名 URL。

发送前 `VideoService.authorize()` 再验单轮权限，`Bot._send_video_receipt()` 仅向 lease 原会话调用 OneBot video helper，不重取会话锁、不吞错误，群回执同步 own-message/proactive 记录。发送阶段使用剩余总 deadline，bridge/JS 发送上限 110 秒，只读 JS 65 秒；并非用更长 lease 延长 DSH 期限。

`video_store.py` 以 conversation/message 为唯一键，canonical platform/ID/part 为身份，preparing 抢占只为避免重复获取；紧邻实际 QQ 动作写 sending，正数回执写 sent，明确平台拒绝写 rejected，发送中取消/断连/超时/缺回执或重启遗留 sending 写 unknown。获取失败与重启遗留 preparing 为 acquisition_failed。重复工具调用只返回原状态，unknown 不自动重试，不承诺 exactly-once。文件 TTL 从发送尝试结束更新，含 unknown 成品保留 10 分钟；单 materialize、256 MiB 预留峰值和保留文件预算使失败关闭而非提前删除上传文件。


### 视频凭据管理

`video_credentials.py` 提供固定平台状态文件的严格解析、0700目录/0600文件、原子替换与cleared tombstone；managed状态与Cookie在同一JSON中一次提交，generation隔离缓存和冷却。`VideoSettings.credentials()` 每次调用选择managed > cleared匿名 > legacy路径，worker通过stdin接收路径和期望generation，内存加载后域约束，不透传给模型。保存或清除后无需重启；并发版本变化失败关闭，不混用新旧Cookie。

`admin_video.py` 与 `admin_ui.py` 提供登录保护的GET安全状态/POST保存删除。写入只允许loopback peer、Host及完整同源Origin，严格UTF-8/JSON/重复头/请求体限额，不接受任意路径或代理自报TLS。React `VideoCredentialsPage` 用既有API与组件，状态刷新不覆盖草稿，保存/清除后清空输入，不把Cookie存浏览器存储。前端状态不能授权，服务端重复校验；既有admin监听配置不因此变更。
