#!/usr/bin/env python3
"""Rewrite bot.env with the current template, preserving existing values.

bot.env only holds secrets and paths. Model selection and parameters live in
the SQLite model registry, so the old LLM_PROVIDER / LLM_MODEL / DSH_PROVIDER /
DSH_MODEL / DSH_MAX_TOKENS lines are dropped on rewrite.
"""

from __future__ import annotations

import os
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/etc/qqbot/bot.env"
KEY_VARS = (
    "LLM_API_KEY",
    "OPENCODE_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "SILICONFLOW_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
    "FISH_API_KEY",
    "STEPFUN_API_KEY",
)

values: dict[str, str] = {}
if os.path.exists(PATH):
    with open(PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()

lines = [
    "# ── Model API keys ───────────────────────────────────────────",
    "# Each registry row names the env var its key comes from (api_key_env).",
    "# The seeded OpenCode rows use OPENCODE_API_KEY.",
]
for name in KEY_VARS:
    lines.append(f"{name}={values.get(name, '')}")
lines += [
    "",
    "# ── Admin UI (localhost + Workbench port forwarding) ──────────",
    f"ADMIN_UI_PASSWORD={values.get('ADMIN_UI_PASSWORD', '')}",
    f"ADMIN_UI_SECRET={values.get('ADMIN_UI_SECRET', '')}",
]
lines += [
    "",
    "# ── Model registry (SQLite, keyed by provider + model_id) ──────",
    "# Active model, context window, max output and reasoning effort live here:",
    "#   main.py --list-models | --use-model p/m | --set-model p/m field=value",
    f"MODEL_DB={values.get('MODEL_DB', '/var/lib/qqbot/models.db')}",
    f"MODEL_PATCH={values.get('MODEL_PATCH', '/var/lib/qqbot/dsh-state/models.patch.json')}",
    "",
    "# ── DSH runtime ─────────────────────────────────────────────",
    f"DSH_HOME={values.get('DSH_HOME', '/var/lib/qqbot/dsh-home')}",
    f"DSH_PROFILE={values.get('DSH_PROFILE', 'qqbot')}",
    f"USER_PROFILE_DB={values.get('USER_PROFILE_DB', '/var/lib/qqbot/dsh-state/profile.db')}",
    "",
    "# ── NapCat OneBot ────────────────────────────────────────────",
    f"ONEBOT_TOKEN={values.get('ONEBOT_TOKEN', '')}",
    "",
    "# ── Admin access (comma-separated QQ numbers) ─────────────────",
    f"ADMIN_QQ_IDS={values.get('ADMIN_QQ_IDS', '')}",
    "",
]

with open(PATH, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
os.chmod(PATH, 0o640)
print(f"rewrote {PATH}")
