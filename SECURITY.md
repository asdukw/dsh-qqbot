# Security

## 报告问题

请通过 GitHub Security Advisories 私下报告安全问题，不要先开公开 issue。

## 部署安全基线

- **使用 QQ 小号。** NapCat 是第三方 NTQQ 客户端，账号可能触发风控或封禁。
- **不要暴露 NapCat 端口。** `6099`（WebUI）和 `3001`（OneBot WS）只绑定 `127.0.0.1`。远程访问 WebUI 请用 SSH 隧道。
- **保护 `bot.env`。** `/etc/qqbot/bot.env` 权限为 `640 root:qqbot`，不要提交到 git。
- **不要在 workspace 放密钥。** 模型对 `/var/lib/qqbot/dsh-workspace` 有读写权限，也能用 `read` / `grep` 看到里面的内容。
- **模型数据外发。** 聊天内容、工具结果、画像会发送给配置的模型提供商；请阅读对应提供商的数据政策。
- **DSH developer preview。** 当前固定 `0.2.1rc2`；升级前先看 release notes 和 profile 兼容性。

## 本项目的缓解措施

- DSH profile 禁用 bash / pwsh / PTY / jobs，只保留 `read` / `write` / `edit` / `glob` / `grep`。
- 文件工具通过 `sandbox-policy` 限制在 `/var/lib/qqbot/dsh-workspace`（`workspace-write`）。
- systemd 单元使用 `ProtectSystem=strict`、`ProtectHome=true`、`ReadWritePaths=/var/lib/qqbot`。
- OneBot WS 使用随机 token；NapCat WebUI 使用随机 token。
- 适配器忽略自己发送的消息，并对重复 `message_id` 去重。

## 已知风险

- QQ 协议实现不是官方支持的方式，账号风险由使用者承担。
- 模型可能把聊天内容写进文件记忆或画像；请只在自己信任的机器上运行。
- 角色卡和世界书是第三方内容，可能包含不适合公开传播的文本。
