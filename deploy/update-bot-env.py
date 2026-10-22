#!/usr/bin/env python3
"""Rewrite bot.env with the full placeholder template, preserving existing values."""

from __future__ import annotations

import os
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/etc/qqbot/bot.env"
KEY_VARS = (
    "OPENCODE_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "SILICONFLOW_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
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
    "# ── LLM provider ─────────────────────────────────────────────",
    "# opencode | deepseek | openai | openrouter | siliconflow | dashscope | moonshot | zhipu | custom",
    f"LLM_PROVIDER={values.get('LLM_PROVIDER', 'opencode')}",
    "# Empty = provider default (opencode: deepseek-v4.1-flash, deepseek: deepseek-flash)",
    f"LLM_MODEL={values.get('LLM_MODEL', '')}",
    "# Single key for the selected DSH route and the fallback client:",
    f"LLM_API_KEY={values.get('LLM_API_KEY', '')}",
    "# Optional provider-specific keys (used when LLM_API_KEY is empty):",
]
for name in KEY_VARS:
    lines.append(f"{name}={values.get(name, '')}")
lines += [
    "# For a custom OpenAI-compatible endpoint only:",
    f"LLM_BASE_URL={values.get('LLM_BASE_URL', '')}",
    "",
    "# ── DSH runtime ─────────────────────────────────────────────",
    f"DSH_HOME={values.get('DSH_HOME', '/var/lib/qqbot/dsh-home')}",
    f"DSH_PROFILE={values.get('DSH_PROFILE', 'qqbot')}",
    f"DSH_PROVIDER={values.get('DSH_PROVIDER', 'opencode')}",
    f"DSH_MODEL={values.get('DSH_MODEL', 'deepseek-v4.1-flash')}",
    f"DSH_MAX_TOKENS={values.get('DSH_MAX_TOKENS', '2048')}",
    "# Context length is per model in the DSH profile, not an env var.",
    f"USER_PROFILE_DB={values.get('USER_PROFILE_DB', '/var/lib/qqbot/dsh-state/profile.db')}",
    "",
    "# ── NapCat OneBot ────────────────────────────────────────────",
    f"ONEBOT_TOKEN={values.get('ONEBOT_TOKEN', '')}",
    "",
]

with open(PATH, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
os.chmod(PATH, 0o640)
print(f"rewrote {PATH}")
