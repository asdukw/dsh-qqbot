# 示例角色卡

## `dafeiyu.json` — 大肥鱼

DeepSeek 的社区二创「蓝色鲸鱼娘」。**不是官方角色卡**：社区里没有公开发行的
SillyTavern PNG/JSON 卡，这张卡是根据公开的 MIT 人设文本整理的 V3 卡。

人设来源：

- [ERUIHNIYHBKBNF/dafeiyu-persona](https://github.com/ERUIHNIYHBKBNF/dafeiyu-persona)（MIT）
  —— `SKILL.md`、`references/lore.md` 的性格、语气规则和梗考据；
- [huaizhuanghub/dsh-persona-dafeiyu](https://github.com/huaizhuanghub/dsh-persona-dafeiyu)
  —— DSH 插件内置的「大肥鱼」人设正文。

整理内容：角色描述、性格、场景、开场白、两段备选开场、三段对话示例、系统提示词、
7 条世界书（吃白饭 / 吃 token、D 指导去吃饭、防火女起源、梁文锋梗、女仆装、
誓死捍卫深度求索、思维链心声）。

安装：

```bash
M="sudo -u qqbot /opt/qqbot/bot/venv/bin/python /opt/qqbot/bot/main.py"
$M --install-card /opt/qqbot/cards/dafeiyu.json
$M --use-card 大肥鱼 --conversation group:123456
```

或者在 QQ 里：

```
/card install /opt/qqbot/cards/dafeiyu.json
/card use 大肥鱼
```

注意：`deploy/install-bot.sh` 会把 `cards/` 复制到 `/opt/qqbot/cards/`，但不会自动
安装；需要显式执行上面的命令。

`/card use` 仅切换命令所在群/私聊；CLI 必须显式指定目标会话。原卡 `first_mes` 会在预告和切换后发送。
