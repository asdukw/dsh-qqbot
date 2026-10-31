"""容器配置初始化；等待面板完成模型设置，不反复重启进程。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
from models import ModelRegistry  # noqa: E402


def _create_once(path: Path, content: bytes) -> None:
    """先完整写入临时文件，再原子创建目标，允许 bot/admin 同时启动。"""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink()


def initialize_config(directory: Path, source: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    config = (source / "config.example.toml").read_text(encoding="utf-8")
    config = config.replace('url = "ws://127.0.0.1:3001"', 'url = "ws://napcat:3001"')
    config = config.replace('host = "127.0.0.1"', 'host = "0.0.0.0"')
    _create_once(directory / "config.toml", config.encode("utf-8"))
    _create_once(directory / "character.json", (source / "character.example.json").read_bytes())


def model_ready(database: str) -> bool:
    registry = ModelRegistry(database)
    try:
        record = registry.active()
        return bool(record and record.enabled and record.provider_enabled and
                    (record.api_key.strip() or os.environ.get(record.api_key_env, "").strip()))
    finally:
        registry.close()


def main() -> None:
    root = Path("/opt/qqbot")
    initialize_config(Path("/etc/qqbot"), root / "bot")
    if os.environ.get("QQBOT_ROLE", "bot") == "admin":
        return
    from main import load_config

    config = load_config("/etc/qqbot/config.toml")
    if config.dsh.enabled:
        home = Path(config.dsh.home)
        profile = home / "profiles" / config.dsh.profile
        if not profile.exists():
            subprocess.run(
                ["dsh", "--profile", config.dsh.profile, "--from-default-profile", "sdk-minimal",
                 "--dump-default-config"],
                env={**os.environ, "DSH_HOME": str(home)}, check=True, stdout=subprocess.DEVNULL,
            )
        # 镜像升级时更新仓库维护的 profile patch，模型路由仍由注册表生成。
        (profile / "cordis.patch.yml").write_bytes((root / "dsh/qqbot.cordis.patch.yml").read_bytes())
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if not model_ready(config.models.database):
        print("Waiting for an active model and API key; configure them in the admin UI.", flush=True)
        while not stop.wait(3):
            if model_ready(config.models.database):
                return
        raise SystemExit(0)


if __name__ == "__main__":
    main()
