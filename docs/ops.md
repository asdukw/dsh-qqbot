# 运维与排障

面向已经按 README 部署好并跑起来的实例。命令默认以 root 执行；直接改动 `/var/lib/qqbot` 下的文件时保持 `qqbot:qqbot` 所有权。所有服务都只监听本机，远程操作请走 SSH 隧道或云厂商的远程连接。

## 服务

| 单元 | 职责 |
|---|---|
| `qqbot.service` | 主进程：OneBot WS、DSH runtime、定时任务、审批执行、日报、订阅轮询 |
| `qqbot-admin.service` | 管理面板，监听 `127.0.0.1:8080`，提供 JSON API 和静态文件 |
| `qqbot-alert.service` | 由 `qqbot.service` 的 `OnFailure=` 触发的单次单元：通过 NapCat 私聊通知管理员（默认 10 分钟冷却） |
| `qqbot-backup.timer` | 每天 04:30（随机延后最多 5 分钟）备份 SQLite |

```bash
systemctl status qqbot qqbot-admin
systemctl list-timers qqbot-backup.timer
journalctl -u qqbot -f
```

主进程是 `Restart=on-failure`，重启间隔 10 秒，300 秒内最多重启 10 次。反复失败会进入 `start-limit-hit`，systemd 不再自动拉起，日志里会看到这个字样：

```bash
systemctl reset-failed qqbot qqbot-admin
systemctl restart qqbot qqbot-admin
```

安装脚本会写入 `deploy/needrestart-qqbot.conf`，避免 `unattended-upgrades` 在一次库更新后连续重启 qqbot 而触发上面的限制。改动后需要 `systemctl daemon-reload`。

## 路径与数据

| 路径 | 内容 | 在默认备份里 |
|---|---|---|
| `/etc/qqbot/bot.env` | 密钥与运行开关，`640 root:qqbot` | 否 |
| `/etc/qqbot/config.toml` | OneBot、回复策略、各模块开关 | 否 |
| `/opt/qqbot` | 代码、venv、`admin/dist` | 否（可从仓库重装） |
| `/var/lib/qqbot/models.db` | 供应商、模型、面板里保存的 API Key | 是 |
| `/var/lib/qqbot/characters.db` | 角色卡库 | 是 |
| `/var/lib/qqbot/memory.db` | 会话状态、`chat_events`、FTS5 检索 | 是 |
| `/var/lib/qqbot/stickers.db` | 表情元数据与 dHash 屏蔽表 | 是 |
| `/var/lib/qqbot/stickers/` | 表情图片文件 | 否 |
| `/var/lib/qqbot/tasks.db` | 定时任务 | 是 |
| `/var/lib/qqbot/usage.db` | Token 账本 | 是 |
| `/var/lib/qqbot/dsh-state/profile.db` | 用户画像、群画像、关系 | 是 |
| `/var/lib/qqbot/feeds.db` | RSS / B 站订阅 | 否 |
| `/var/lib/qqbot/approvals.db` | 好友 / 群请求审批 | 否 |
| `/var/lib/qqbot/daily.db` | 日报发送状态 | 否 |
| `/var/lib/qqbot/dsh-home/sessions/` | DSH 会话 JSONL | 否 |
| `/var/lib/qqbot/dsh-workspace/` | 模型可读写工作区（`notes/`、`spill/`；世界书通过会话 prompt 注入） | 否 |
| `/var/lib/qqbot/backups/` | 每日备份归档 | — |
| `/opt/qqbot/napcat/` | NapCat compose、`.env`、QQ 数据 | 否 |

`bot.env` 和 `config.toml` 不在备份里，请单独留存副本。表情图片和 DSH session 也不在备份里：前者删了还能重新收藏，后者只影响模型回看旧会话。

## 日常检查

```bash
M="sudo -u qqbot env HOME=/var/lib/qqbot /opt/qqbot/bot/venv/bin/python /opt/qqbot/bot/main.py --config /etc/qqbot/config.toml"

$M --check-dsh        # 用当前激活模型真实跑一轮 DSH，验证 profile、密钥和网络
$M --check-onebot    # 连 NapCat 并取登录信息，不发消息
$M --check            # 测试模型 API；[dsh] enabled = false 时走直连客户端，否则等同于 --check-dsh
```

`main.py` 会自己读取 `/etc/qqbot/bot.env`，不需要手动 `source`。面板可用 `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/healthz` 检查，返回 `200` 即正常；`/api/*` 在未登录时返回 `401` 是预期行为（返回 `404` 说明后端没跑起来）。

## 日志

- 主进程：`journalctl -u qqbot --since "2 hours ago"`，`SyslogIdentifier=qqbot`；
- 面板：`journalctl -u qqbot-admin`，登录失败、静态文件缺失、API 异常都在这里；
- 告警：`journalctl -u qqbot-alert`，可以确认告警是否发出、是否被冷却抑制；
- 备份：`journalctl -u qqbot-backup`；
- NapCat：`cd /opt/qqbot/napcat && docker compose logs --tail=200 napcat`；
- DSH runtime 的启动、崩溃和模型错误也在 `qqbot` 单元里，搜索 `dsh` 或 `turn/end`。

日志级别固定为 INFO（`websockets` / `httpx` 降到 WARNING），没有运行时开关。需要细节时用仓库里的 `deploy/napcat/diagnostics/` 脚本（在服务器上从仓库目录运行，脚本按 `/opt/qqbot/bot` 导入模块）：`check_debug.py` 带应用 DEBUG 连接 NapCat，`ws_probe.py` 探测 WS 消息，`test_endpoints.sh` 直接打模型端点。诊断脚本会保持 `websockets` 为 WARNING，避免握手 DEBUG 输出 Authorization 请求头；诊断输出仍只应在受限的运维环境中查看，不要直接公开分享。OneBot 凭据应使用独立 token 配置，不要嵌入 URL；接受 token 参数的探测脚本还有命令历史/进程参数暴露风险。

消息处理、普通/主动回复及其图片、文本/媒体发送日志带本地 `rid=<12 位十六进制>`；同轮的 LLM 重试和 `asyncio.to_thread` 中的 DSH 适配器日志沿用它，可用 `journalctl -u qqbot | grep 'rid=...'` 串联排障。无消息上下文时为 `rid=-`。ID 不进入 QQ 回复、模型 prompt 或数据库；SDK 自建线程、DSH 子进程和独立后台定时任务不保证有同一 ID。`reply processing finished` 只表示回复处理流程结束，不代表所有文本分片/媒体已送达；发送失败仍有单独 ERROR。

面板访问日志只记录白名单方法、路由类别和状态，不记录原始 URL/query、请求体、Cookie 或 Authorization。`admin login outcome=success` 表示会话已签发；错误密码为 WARNING，进入限流状态时每个来源每窗口记录一次 WARNING，后续 429 只保留普通访问日志。密码未配置的逐请求事件为 DEBUG，启动时仍有 WARNING。来源使用直连 IP，不信任 X-Forwarded-For。

### 日志容量与保留

主进程和面板使用 systemd journal；服务单元将 stdout/stderr 交给 journal（`deploy/qqbot.service`），不需要再给 Python 增加文件日志或用 logrotate 处理 journal 文件。部署后先只读检查主机边界：

```bash
journalctl --disk-usage
systemd-analyze cat-config systemd/journald.conf
cd /opt/qqbot/napcat && docker compose logs --tail=200 napcat
docker system df
```

仓库没有替生产主机设置 journald 的容量和保留期限。若需要显式限制，请由主机管理员评估共享影响后添加 journald drop-in，例如 `[Journal]` 中的 `SystemMaxUse=200M`、`RuntimeMaxUse=50M`、`SystemKeepFree=1G` 和 `MaxRetentionSec=14day`；这些是示例，不代表当前有效配置，也不能保证至少保留 14 天：容量和时间限制先触发者优先。确认 `/var/log/journal`（持久日志）与 `/run/log/journal`（运行期日志）的实际配置后，再重启 `systemd-journald` 并复查占用。

NapCat 的 Docker 日志、DSH 会话 JSONL 和 SQLite 数据均不受 journald 配额控制。Docker 轮转也不限制容器内另写的文件。当前 `deploy/napcat/setup-napcat.sh` 没有显式设置 Docker 日志轮转；如主机策略需要，可在 Compose 服务中由管理员加入 `json-file` 的 `max-size` / `max-file`，但变更需要重建容器，可能造成短暂断连，不由此文档自动执行。

清理日志是破坏性操作：`journalctl --vacuum-time` / `--vacuum-size` 作用于整台主机的归档 journal，不是只删除 `qqbot`，也不会删除仍活动的 journal 文件；执行前先保留必要的排障记录，并确认不会影响其他服务。

## 备份与恢复

`qqbot-backup.timer` 以 `qqbot` 用户调用 `bot/backup.py`，用 SQLite 在线备份 API 复制数据库（WAL 下也一致），打包到 `/var/lib/qqbot/backups/qqbot-backup-<时间戳>.tar.gz`，默认保留 14 天。默认包含 `models.db`、`characters.db`、`memory.db`、`stickers.db`、`tasks.db`、`usage.db` 和 `dsh-state/profile.db`。

```bash
systemctl start qqbot-backup.service          # 立即备份一次
journalctl -u qqbot-backup -n 20
tar -tzf /var/lib/qqbot/backups/qqbot-backup-*.tar.gz | less
```

需要把 `feeds.db` / `approvals.db` / `daily.db` 也纳入时，用 drop-in 覆盖 `ExecStart` 并显式列出所有数据库（参数是完整的数据库路径）：

```bash
systemctl edit qqbot-backup.service
```

```ini
[Service]
ExecStart=
ExecStart=/opt/qqbot/bot/venv/bin/python /opt/qqbot/bot/backup.py --keep-days 14 /var/lib/qqbot/models.db /var/lib/qqbot/characters.db /var/lib/qqbot/memory.db /var/lib/qqbot/stickers.db /var/lib/qqbot/tasks.db /var/lib/qqbot/usage.db /var/lib/qqbot/feeds.db /var/lib/qqbot/approvals.db /var/lib/qqbot/daily.db /var/lib/qqbot/dsh-state/profile.db
```

恢复时先停服务，避免进程继续写旧句柄：

```bash
systemctl stop qqbot qqbot-admin
work=$(mktemp -d)
tar -xzf /var/lib/qqbot/backups/qqbot-backup-YYYYmmdd-HHMMSS.tar.gz -C "$work"
for db in models characters memory stickers tasks usage; do
    [ -f "$work/$db.db" ] && install -o qqbot -g qqbot -m 600 "$work/$db.db" "/var/lib/qqbot/$db.db"
done
[ -f "$work/profile.db" ] && install -o qqbot -g qqbot -m 600 "$work/profile.db" /var/lib/qqbot/dsh-state/profile.db
rm -rf "$work"
rm -f /var/lib/qqbot/*.db-wal /var/lib/qqbot/*.db-shm /var/lib/qqbot/dsh-state/*.db-wal /var/lib/qqbot/dsh-state/*.db-shm
systemctl start qqbot qqbot-admin
```

归档里的数据库都在同一层（`profile.db` 不在子目录），所以恢复时要显式放回 `dsh-state/`。

## 常见故障

**进程健康不等于 QQ 在线。** `/healthz` 只检查面板存活；`/readyz` 会携带服务器端凭据探测 OneBot `get_status`，只有账号 online 且无明确故障时返回200，断连/待扫码返回503（10秒缓存，5秒探测上限），不返回token或原始错误。系统页显示实际链路状态，部署检查也必须检查readyz。QQ登录失效时NapCat容器可能仍是running，systemd不会触发OnFailure，且不能依赖同一失效QQ链路私聊告警；外部可监测readyz。

2026-10-26 10:53:47（Asia/Shanghai）事故确认：NapCat `KickedOffLine` 显示登录失效，10:53:51 OneBot断开，此后需扫码；无服务退出或OOM证据。不要用循环重启企图绕过QQ重新授权。

**QQ 掉线或提示重新登录。** 主进程日志会反复出现 OneBot 重连。检查 `cd /opt/qqbot/napcat && docker compose ps`，然后 `napcat-qr` 打印现有二维码；过期或已失效时用 `napcat-qr --fresh` 重启 NapCat 强制生成新码。扫码后不需要重启 qqbot。

**主进程起不来或反复退出。** 先看 `journalctl -u qqbot -n 100`：TOML 解析错误、数据库权限、激活模型没 key 都会在启动阶段报出来。修好后 `systemctl reset-failed qqbot && systemctl restart qqbot`。

**面板打不开或一直 401。** 面板只监听 `127.0.0.1`，远程要先做端口转发（SSH 隧道或云厂商的端口转发）。登录被拒时检查 `ADMIN_UI_PASSWORD` 是否写进 `bot.env`；未设置时登录接口会直接返回「未设置 ADMIN_UI_PASSWORD」。设置过 `[admin] allow_cidrs` 时确认来源 IP 在允许范围内（经隧道访问时来源是 `127.0.0.1`）。

**模型报 401 / 403 / 429。** 401 是密钥错误或没读到：面板保存的 key 在 `models.db`，`bot.env` 里的环境变量是另一条来源，模型页会给每个供应商标出密钥来源（`stored` / `env` / `missing`）。403 常见于 `deepseek-v4.1-flash-free`，这个模型只能在 OpenCode 客户端内用。429 是限流或配额，到面板「Token 用量」按用途和会话看消耗。

**DSH runtime 崩溃。** adapter 会捕获异常、关闭旧 runtime，下一轮消息再启动，session JSONL 不丢。如果连续崩溃，先跑 `--check-dsh`，再看 `/var/lib/qqbot/dsh-state/models.patch.json` 是否被写成了非法内容，必要时删掉让启动时按注册表重新渲染。

**审批一直停在 `executing`。** bot 重启时会把中断的行重新排队，正常情况下等一次重启即可。长期卡住说明 OneBot 侧调用没有返回，重启 `qqbot` 并看日志里的 `set_friend_add_request` / `set_group_add_request` 错误；失败的记录可以在面板里重试。

**日报没发。** 依次检查 `[daily] enabled`、`hour`/`minute`、`min_messages`（消息太少的会话会被标记 `skipped`），以及 `daily.db` 里的状态和尝试次数。超过 `max_late_hours` 的日期不再补发，需要的话用管理员 `/daily YYYY-MM-DD` 手动发。

**订阅不转发。** `/sub list` 会显示每条订阅的 `last_error`。B 站对机房 IP 返回 `-412`，换一个 RSSHub 镜像（`[feeds] rsshub_base`）；`/sub check` 可以立即抓一次。

**`/sub add` 返回 `feed HTTP 503`。** 这通常是 RSSHub 镜像的临时错误：上游 B 站风控（`-412`）或镜像的 browser helper 不可用。添加时遇到 `408/425/429/500/502/503/504` 或网络异常，最多额外重试两次，间隔 1 秒、3 秒；轮询最多额外重试一次。抓取或 XML 解析失败、添加成功、重复添加都会写入日志：`journalctl -u qqbot | grep subscription`（HTTP 失败行带 `status=` 和错误页的诊断摘要，超时会包含 `ReadTimeout` 等异常类型）。日志中的 URL 用户名、密码、查询参数和片段会脱敏，不记录可能回退为 URL 的订阅标题。仍然失败就换镜像或错峰重试；也可从服务器请求同一 URL 验证。如果进程设置了出站代理，可将 RSSHub 域名追加到 `bot.env` 的 `NO_PROXY`，保留原有条目并重启服务；这只改变该域名的代理选择，不会修复镜像自身的 503。

**语音或梗图不生效。** 两者都有每会话 30 秒冷却，先确认不是冷却。再看 `[voice]` / `[meme]` 的 provider 和文件目录；合成或绘制失败时 adapter 会退回文字，日志里有对应错误。

## 升级与回滚

新代码就位后重跑安装脚本即可，它会重建 venv、同步 `admin/dist`、重写 systemd 单元并启用备份 timer：

```bash
sudo bash deploy/install-bot.sh "$PWD"
systemctl restart qqbot qqbot-admin
```

升级前手动跑一次 `systemctl start qqbot-backup.service`。`deepseek-harness-sdk` 固定为 `0.2.1rc2`，升级前先确认 profile 兼容性，并用 `dsh --profile qqbot --dump-config` 检查渲染结果。

回滚就是回到上一个提交再跑一次安装脚本；所有数据库都不在代码目录里，不受影响。面板单独出问题时同样整体重跑，不需要额外步骤。

## 安全底线

- NapCat 的 `6099`（WebUI）和 `3001`（OneBot WS）、面板的 `8080` 都只监听 `127.0.0.1`；不要为了远程访问改成 `0.0.0.0`。
- 不要手工删除 `/var/lib/qqbot` 下的数据库；需要清理时先备份。
- workspace 里的内容模型都能读到，不要放密钥或隐私文件。
- QQ 账号风险、数据外发范围见 [SECURITY.md](../SECURITY.md)。

## 会话角色切换

安装与切换分开，现有默认卡不变：

```bash
$M --install-card /path/to/card.png
$M --use-card 2 --conversation group:123456
$M --switch-status <任务ID>
```

重复 `--conversation` 可向明确指定的会话 fanout。在线 bot 等旧回复全部发完后，发送预告、切换目标会话并发送原 `first_mes`。不会自动枚举群/好友，不发 `@全体成员`。

任务 `unknown` 表示发送超时/中断，QQ 是否已收到无法确定；`paused` 表示已确认部分步骤但尚未完成。不要直接重建任务或清空数据库。检查 `receipts` 和目标会话后：

- `--resume-switch <任务ID>`：显式允许继续，已保存回执的分片不重发；未知分片可能重复，必须先人工核实。
- `--resume-switch <任务ID> --opening-delivered`：仅当已经绑定且确认完整开场白已投递时，直接完成本地状态，不发送消息。

未完成切换会暂停目标的角色输出，不影响其他会话。`/reset` 只轮换当前 session，保留角色绑定。升级前备份 `characters.db`、`memory.db`、`profile.db`；旧自动生成 `memory/lore.md` 应移出模型 workspace 备份，用户 notes 不动。权限下放不属于此功能，现有管理员鉴权保持不变。
