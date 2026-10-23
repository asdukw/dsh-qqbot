"""Notify the admins when the qqbot service fails.

Wired through `OnFailure=qqbot-alert.service`, so systemd runs this as a
one-shot after qqbot exits abnormally (including start-limit-hit). Talks to
NapCat over the same local OneBot websocket the bot uses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import tomllib
from pathlib import Path
from typing import Any

import websockets

BOT_ENV = "/etc/qqbot/bot.env"
CONFIG = "/etc/qqbot/config.toml"
STATE = "/var/lib/qqbot/.last-alert"
DEFAULT_COOLDOWN = 600


def read_env(path: str = BOT_ENV) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def load_config() -> dict[str, Any]:
    try:
        with Path(CONFIG).open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def admin_ids(env: dict[str, str], config: dict[str, Any]) -> list[int]:
    raw = str(env.get("ADMIN_QQ_IDS") or "")
    if not raw:
        admins = (config.get("bot") or {}).get("admins") or []
        raw = " ".join(str(item) for item in admins)
    ids: list[int] = []
    for part in raw.replace(",", " ").split():
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return sorted(set(ids))


def failure_text() -> str:
    result = os.environ.get("SERVICE_RESULT") or "unknown"
    code = os.environ.get("EXIT_CODE") or ""
    status = os.environ.get("EXIT_STATUS") or ""
    detail = " ".join(part for part in (result, code, status) if part)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    return f"qqbot 服务异常退出（{detail}），时间 {stamp}。请登录 ECS 检查。"


def cooldown_ok(cooldown: int, force: bool) -> bool:
    if force or cooldown <= 0:
        return True
    try:
        last = float(Path(STATE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return time.time() - last >= cooldown


def mark_sent() -> None:
    try:
        Path(STATE).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


async def call(ws: Any, action: str, params: dict, echo: int) -> dict:
    await ws.send(json.dumps({"action": action, "params": params, "echo": str(echo)}))
    while True:
        payload = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if payload.get("echo") == str(echo):
            return payload


async def notify(url: str, token: str, admins: list[int], text: str) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}access_token={token}"
    sent = 0
    async with websockets.connect(
        url, additional_headers=headers, open_timeout=10, close_timeout=5
    ) as ws:
        for index, user_id in enumerate(admins, start=1):
            try:
                reply = await call(
                    ws,
                    "send_private_msg",
                    {
                        "user_id": user_id,
                        "message": [{"type": "text", "data": {"text": text}}],
                    },
                    index,
                )
                if reply.get("retcode") in (0, None):
                    sent += 1
            except Exception as exc:  # noqa: BLE001 - try the next admin
                print(f"alert to {user_id} failed: {exc}")
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="notify admins that qqbot failed")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN)
    parser.add_argument("--message", default="", help="override the alert text (testing)")
    args = parser.parse_args(argv)

    env = read_env()
    config = load_config()
    admins = admin_ids(env, config)
    if not admins:
        print("no admin ids configured (ADMIN_QQ_IDS or [bot] admins)")
        return 1
    if not cooldown_ok(args.cooldown, args.force):
        print("alert suppressed by cooldown")
        return 0

    url = str((config.get("onebot") or {}).get("url") or "ws://127.0.0.1:3001")
    token = env.get("ONEBOT_TOKEN", "")
    text = args.message.strip() or failure_text()
    try:
        sent = asyncio.run(notify(url, token, admins, text))
    except Exception as exc:  # noqa: BLE001 - NapCat itself may be down
        print(f"alert failed: {exc}")
        return 1
    if sent:
        mark_sent()
    print(f"alert sent to {sent}/{len(admins)} admin(s)")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
