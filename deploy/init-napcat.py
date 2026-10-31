"""首次启动时初始化 NapCat OneBot 配置，不覆盖已有账号配置。"""

import json
import os
from pathlib import Path


def initialize(directory: Path, token: str) -> None:
    if not token:
        raise ValueError("ONEBOT_TOKEN must be set")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "onebot11.json"
    configs = list(directory.glob("onebot11*.json"))
    if configs:
        for config in configs:
            data = json.loads(config.read_text(encoding="utf-8"))
            servers = data.get("network", {}).get("websocketServers", [])
            if not any(
                server.get("enable")
                and server.get("port") == 3001
                and server.get("host") == "0.0.0.0"
                and server.get("token") == token
                for server in servers
            ):
                raise ValueError("Existing NapCat config does not match OneBot settings")
        return
    data = {
        "network": {
            "httpServers": [],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [{
                "enable": True,
                "name": "ws",
                "host": "0.0.0.0",
                "port": 3001,
                "reportSelfMessage": False,
                "enableForcePushEvent": True,
                "messagePostFormat": "array",
                "token": token,
                "debug": False,
                "heartInterval": 30000,
            }],
            "websocketClients": [],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    initialize(Path("/napcat-config"), os.environ.get("ONEBOT_TOKEN", ""))
