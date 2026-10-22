#!/usr/bin/env bash
# Prepare and start NapCat (Docker). Run as root:  setup-napcat.sh
set -euo pipefail

BASE=/opt/qqbot/napcat
install -d -m 755 "$BASE" "$BASE/data" "$BASE/data/qq" "$BASE/data/config" "$BASE/data/plugins"

if [ ! -f "$BASE/.env" ]; then
    umask 077
    {
        echo "WEBUI_TOKEN=$(openssl rand -hex 24)"
        echo "ONEBOT_TOKEN=$(openssl rand -hex 24)"
    } > "$BASE/.env"
fi
chmod 600 "$BASE/.env"
set -a
# shellcheck disable=SC1091
. "$BASE/.env"
set +a

cat > "$BASE/compose.yaml" <<'YAML'
services:
  napcat:
    image: mlikiowa/napcat-docker:latest
    container_name: napcat
    restart: unless-stopped
    environment:
      MODE: ws
      WEBUI_TOKEN: ${WEBUI_TOKEN}
      NAPCAT_UID: "0"
      NAPCAT_GID: "0"
    ports:
      - "127.0.0.1:6099:6099"
      - "127.0.0.1:3001:3001"
    volumes:
      - ./data/qq:/app/.config/QQ
      - ./data/config:/app/napcat/config
      - ./data/plugins:/app/napcat/plugins
YAML

if [ ! -f "$BASE/data/config/onebot11.json" ]; then
    python3 - "$BASE/data/config/onebot11.json" "$ONEBOT_TOKEN" <<'PY'
import json
import sys

path, token = sys.argv[1], sys.argv[2]
config = {
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
with open(path, "w", encoding="utf-8") as fh:
    json.dump(config, fh, ensure_ascii=False, indent=2)
PY
fi
chmod 600 "$BASE/data/config/onebot11.json"

cd "$BASE"
docker compose up -d
docker compose ps

install -m 755 "$(dirname "$0")/napcat-qr.sh" /usr/local/bin/napcat-qr
