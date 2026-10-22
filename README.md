# DSH QQ Bot

> 一个把 **DeepSeek Harness（DSH）** 接到 **QQ** 上的聊天机器人。NapCat 负责 QQ 协议，DSH 负责 agent runtime、会话记忆、文件工具和可插拔的画像插件。

项目还处于早期阶段。DSH 本身是 developer preview，当前固定 `0.2.1rc2`，升级可能带来不兼容变更。

## 特性

- **QQ 私聊聊天**，群聊默认只回复 @ 消息（`reply_group_at = false` 可关闭）
- **角色卡**：SillyTavern Character Card V1 / V2 / V3，支持 `.json` 和 `.png`（读取 `ccv3` → `chara` → `iTXt`）
- **DSH session**：每个 QQ 会话一份持久化 JSONL 历史，自动上下文压缩
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

至少填这几项：

```ini
LLM_API_KEY=sk-...
LLM_PROVIDER=opencode
LLM_MODEL=
DSH_PROVIDER=opencode
DSH_MODEL=deepseek-v4.1-flash
```

说明：

- `DSH_PROVIDER` / `DSH_MODEL` 是机器人实际使用的模型路由，默认是 OpenCode Zen 的 `deepseek-v4.1-flash`。
- `LLM_API_KEY` 是 DSH 路由和 adapter 回退路径共用的单个 key；也可以按 provider 填 `OPENCODE_API_KEY` / `DEEPSEEK_API_KEY` 等，`LLM_API_KEY` 优先。
- 想换 provider：pi-ai 已内置的 route（如 `openai`、`deepseek`）可以直接用；没有内置的中转需要在 `dsh/qqbot.cordis.patch.yml` 的 `llm-pi-ai.providers` 里加一个 custom route，并让 `DSH_MODEL` 与 route 的 models 列表一致。
- `bot/llm.py` 里的 base URL 预设只用于 adapter 的薄客户端回退路径（`DSH_ENABLED=false` 时）。

> `deepseek-v4.1-flash-free` 只能在 OpenCode 客户端内使用；从机器人直接调用会返回 `403 FreeTierError`。机器人请使用付费的 `deepseek-v4.1-flash`，或换一个 provider。

### 4. 启动

```bash
sudo systemctl start qqbot
sudo journalctl -u qqbot -f
```

给小号发一条私聊消息，确认回复。

## 角色卡

把角色卡放到 `/etc/qqbot/character.json`（或 `.png`），然后重启：

```bash
sudo systemctl restart qqbot
```

- 支持 V1 平铺字段、V2 `chara_card_v2`、V3 `chara_card_v3`；
- PNG 卡会依次查找 `ccv3`、`chara` 的 `tEXt` / `iTXt` 块；
- 卡里的 `description` / `personality` / `scenario` / `mes_example` / `system_prompt` 会渲染成 DSH 的系统提示词；
- 世界书条目会写到 `memory/lore.md`，模型用 `grep` / `read` 查；
- 换卡会让 DSH runtime 重启，但 session 历史保留。

## 记忆

三层：

1. **会话记忆**：DSH session JSONL，位于 `/var/lib/qqbot/dsh-home/sessions`，每个 QQ 会话一份，自动压缩。
2. **用户画像**：SQLite `/var/lib/qqbot/dsh-state/profile.db`，表 `facts(session_id, key, value, confidence, source, created_at, updated_at)`；插件把画像注入每一轮的 system prompt。
3. **文件记忆**：`/var/lib/qqbot/dsh-workspace/memory/`；`lore.md` 是角色设定，`notes/` 放事件、约定和重要片段。模型可以用 `write` / `edit` / `glob` / `grep` 维护和检索。

画像默认按 QQ 会话隔离（私聊和群聊各自一份）。如果以后要跨会话共享，可以把 `session_id` 映射成稳定的用户 id。

## 配置

| 路径 | 用途 |
|---|---|
| `/etc/qqbot/bot.env` | 密钥和运行开关，`640 root:qqbot` |
| `/etc/qqbot/config.toml` | OneBot、LLM、DSH、回复策略、路径 |
| `/var/lib/qqbot/dsh-home/profiles/qqbot/cordis.patch.yml` | DSH profile 的插件选择 |
| `/etc/qqbot/character.json` | 角色卡 |
| `/var/lib/qqbot/dsh-workspace/` | 模型可读写的记忆工作区 |

`bot/config.example.toml` 里有完整字段和注释。群聊默认只回复 @ 消息；不想在群里说话时把 `[bot] reply_group_at` 设为 `false`。

## 上下文与压缩

- **上下文长度**写在 DSH route 的模型条目里（`dsh/qqbot.cordis.patch.yml` 的 `contextWindow`）：付费 Flash 是 128k，free 是 200k。模型不需要在 prompt 里被告知上下文长度；DSH 的 token meter/compaction 直接读这个元数据。
- `DSH_CONTEXT_WINDOW` 环境变量只用于直连 DeepSeek adapter 的兜底；我们的 `llm-pi-ai` 自定义 route 以模型条目的 `contextWindow` 为准。
- `compaction-basic` 默认在 80% 压力时自动压缩。本项目显式配置为：

```yaml
thresholdRatio: 0.8        # 目标 80%
headroomTokens: 16384      # 额外预留
retainRatio: 0.16          # 压缩后保留约 16% 的最近上下文
auto: true
```

- 实际触发点 = `min(0.8 × contextWindow, contextWindow − 输出预留 − headroomTokens)`。配合 `max_tokens = 2048`，200k 窗口下约为 160k（80%）。
- 压缩会把较旧的历史总结成摘要，保留最近的上下文；完整 session 仍以 JSONL 存在 `/var/lib/qqbot/dsh-home/sessions`。
- 摘要默认使用同一个 provider/model；需要的话可以在 `compaction-basic` 里用 `summarizationProvider` / `summarizationModel` 单独指定。
- `DSH_MAX_TOKENS`（或 `[dsh] max_tokens`）是单次输出上限，也会影响压缩时的输出预留。

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
deploy/     NapCat/Docker、systemd、安装脚本、诊断工具
dsh/        DSH 自定义 profile patch、user-profile 插件、fake LLM 测试服务
docs/       架构说明
tests/      单元测试（卡片解析、prompt）
```

## 开发

```bash
python -m unittest discover -s tests -v
python -m py_compile bot/*.py
```

本地调试 DSH 工具链时，可以不用真实模型：`dsh/fake_llm.py` 是一个 OpenAI-compatible 的假服务，会先让模型调用 `profile_write`，再返回文本。profile patch 里已经带了一个指向 `127.0.0.1:8000` 的 `fake` 路由。

## Roadmap

定位是社交娱乐型 QQ bot，后续功能不会很多，可能包括：

- 群聊互动 / 小游戏
- 表情、图片、语音消息
- 主动搭话 / 定时问候
- 更丰富的长期记忆（跨会话画像、事件检索）

## License

MIT
