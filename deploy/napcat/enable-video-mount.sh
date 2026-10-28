#!/usr/bin/env bash
# Explicit existing-host upgrade: add only the video outbox mount and recreate NapCat.
# Run as root after reviewing downtime: enable-video-mount.sh --apply [compose-file]
set -euo pipefail
if [ "${1:-}" != --apply ]; then
    echo 'Usage: enable-video-mount.sh --apply [compose-file]' >&2
    echo 'Recreates NapCat; QQ is briefly disconnected. No database or login-state deletion.' >&2
    exit 2
fi
COMPOSE="${2:-/opt/qqbot/napcat/compose.yaml}"
[ -f "$COMPOSE" ] || { echo 'Compose file not found' >&2; exit 1; }
install -d -m 750 /var/lib/qqbot/video-media /var/lib/qqbot/video-media/outbox
# Preserve interpolations; never print the resolved service environment or secrets.
TMP=$(mktemp "${COMPOSE}.video.XXXXXX")
trap 'rm -f "$TMP"' EXIT
chmod 600 "$TMP"
docker compose -f "$COMPOSE" config --no-interpolate --format json > "$TMP"
python3 - "$TMP" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
config = json.loads(path.read_text())
service = config['services']['napcat']
volumes = service.setdefault('volumes', [])
source, target = '/var/lib/qqbot/video-media/outbox', '/app/qqbot-video'
for item in volumes:
    if not isinstance(item, dict):
        raise SystemExit('Unsupported Compose volume representation; no changes made')
    if item.get('target') == target:
        if item.get('type') != 'bind' or item.get('source') != source or not item.get('read_only'):
            raise SystemExit('Conflicting video mount; no changes made')
        break
else:
    volumes.append({'type': 'bind', 'source': source, 'target': target, 'read_only': True})
# JSON is valid YAML. Keep the remaining normalized Compose settings unchanged.
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
PY
docker compose -f "$TMP" config --quiet
# Back up only this explicitly selected configuration; do not overwrite earlier backups.
if [ ! -e "${COMPOSE}.before-video" ]; then
    cp -p "$COMPOSE" "${COMPOSE}.before-video"
    chmod 600 "${COMPOSE}.before-video"
fi
mv "$TMP" "$COMPOSE"
chmod 600 "$COMPOSE"
docker compose -f "$COMPOSE" up -d --no-deps --force-recreate napcat
docker exec napcat test -r /app/qqbot-video
echo 'Video outbox mounted. Verify QQ/OneBot readiness before testing delivery.'
