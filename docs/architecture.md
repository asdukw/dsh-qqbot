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
                       (OpenCode Zen)        (JSONL, 自动压缩)        (SQLite)
```

### adapter（`bot/`）

职责：

- 维护到 NapCat 的 OneBot 11 正向 WebSocket：token 鉴权、重连、心跳、echo 关联；
- 解析消息段（text / at / image / face / record 等），去重；
- 决定是否回复：私聊全回，群聊只在被 @ 且 `reply_group_at = true` 时回复；
- 把 QQ 会话映射成稳定的 DSH `session_id`；
- 角色卡加载、系统提示词构建、世界书落盘；
- 长回复拆分、发送失败日志；
- 本地 SQLite 消息审计（不是 DSH 的上下文来源）。

不负责：模型调用、历史管理、工具执行、画像存储。这些在 DSH 里。

### DSH profile（`dsh/qqbot.cordis.patch.yml`）

从 DSH 的 `sdk-minimal` 模板派生，保留：

- `sdk-app-startup` / `sdk-jsonrpc-server`
- `llm-pi-ai`（OpenCode Zen 路由）
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
2. adapter 提取文本、判断是否需要回复、生成 `conversation_id`：
   - 私聊：`private:<user_id>`
   - 群聊：`group:<group_id>:<user_id>`
3. `session_id = conversation_id.replace(":", "-")`。
4. 如果角色卡 mtime 变化：重新生成系统提示词、写 `memory/lore.md`、重启 DSH runtime（session 保留）。
5. `asyncio.to_thread(dsh.run, session_id, text)`：
   - DSH 启动/复用 runtime；
   - 加载或创建 `session_id` 的 JSONL 历史；
   - 组装 system prompt（含画像变量）；
   - 跑 agent loop；
   - 如果模型调用 `profile_*` 或文件工具，执行并把结果回灌；
   - 返回 `final_response`。
6. adapter 按 `max_reply_chars` 拆分并发送。
7. 用户消息和助手回复同时写入本地 SQLite `messages` 表，仅作审计。

## 用户画像插件（`dsh/user-profile.mjs`）

- 在 `agent/created` 事件里，为每个 agent 注册 **agent-scoped** 的 system prompt 变量和 section：
  - 变量 `user_profile` 从 SQLite 读当前 `session_id` 的 facts；
  - section `user-profile`（order 8000）把变量插入系统提示词；
  - 只影响当前 agent，不会把一个用户的画像泄露给另一个用户。
- 注册全局工具，但用 `exec.agent.id` 区分 session：
  - `profile_read`：读一条或全部；
  - `profile_write`：upsert 一条稳定事实；
  - `profile_forget`：删除一条。
- 存储：`/var/lib/qqbot/dsh-state/profile.db`

```sql
CREATE TABLE facts (
  session_id TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  source     TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (session_id, key)
);
```

画像只保存用户明确说出的稳定事实。事件、约定和重要片段走文件记忆（`memory/notes/`），由模型用 `write` / `edit` / `glob` / `grep` 维护。

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
