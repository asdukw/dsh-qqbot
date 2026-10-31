# dsh-qqbot — AGENTS.md

QQ 聊天机器人：DeepSeek Harness（DSH）+ NapCat / OneBot。生产环境是 Ubuntu + Docker Compose；
仓库本身是可部署的 Python 服务，Python ≥ 3.12，用 uv 管理依赖。功能和配置细节在 [README.md](README.md)。

## 目录
- `bot/`：OneBot 适配、DSH client、角色卡、SQLite、prompt 回退
- `cards/`：示例角色卡；`deploy/`：NapCat / Docker / systemd / 安装与诊断
- `dsh/`：DSH 自定义 profile patch、插件、fake LLM 测试服务
- `tests/`：单元测试；`docs/`：架构、运维、音色

## 开发与门禁
```bash
uv venv
uv pip install -r bot/requirements.txt -r requirements-dev.txt
python -m unittest discover -s tests -v
ruff check .
pyright
```
提交前三条检查全绿。ruff / pyright 配置在 `pyproject.toml`；生产代码按 standard 模式类型检查。

## 约定
- 角色卡只接受中文卡：安装或切换前确认 `description` / `first_mes` 为中文；英文卡（含英文原卡）不安装。
- 代码注释、docstring、文档用中文；提交信息用英文，沿用仓库既有 imperative 风格（如 `Add ...`、`Improve ...`）。
- 密钥和 token 从环境变量或配置读取，不写死、不入库；`.env` 已被忽略。
- 改 DSH profile / 插件前先读 [docs/architecture.md](docs/architecture.md)；部署与排障先读 [docs/ops.md](docs/ops.md)。
- 本地调 DSH 可用 `dsh/fake_llm.py`，不需要真实模型。
