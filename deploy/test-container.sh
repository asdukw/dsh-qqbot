#!/usr/bin/env bash
# 只构建/运行本地测试资源；不读取 .env、不拉取 NapCat、不访问真实供应商。
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export QQBOT_IMAGE=${QQBOT_IMAGE:-qqbot:local}
export ADMIN_UI_PASSWORD=dummy-admin-password ONEBOT_TOKEN=dummy-onebot-token NAPCAT_WEBUI_TOKEN=dummy-webui-token
PROJECT="qqbot-smoke-$(date +%s)-$$"
ENV_FILE=$(mktemp)
COMPOSE_FILE="$ROOT/compose.yaml"
SMOKE_FILE="$ROOT/tests/container-smoke.compose.yaml"
if command -v cygpath >/dev/null 2>&1; then
  export MSYS_NO_PATHCONV=1
  ENV_FILE_NATIVE=$(cygpath -w "$ENV_FILE")
  COMPOSE_FILE=$(cygpath -w "$COMPOSE_FILE")
  SMOKE_FILE=$(cygpath -w "$SMOKE_FILE")
else
  ENV_FILE_NATIVE="$ENV_FILE"
fi
COMPOSE=(docker compose --env-file "$ENV_FILE_NATIVE" -p "$PROJECT" -f "$COMPOSE_FILE" -f "$SMOKE_FILE")
cleanup() {
  "${COMPOSE[@]}" logs --no-color
  # 项目名是本次运行生成的，只清理本脚本创建的容器和命名卷。
  "${COMPOSE[@]}" down --volumes
  rm -f "$ENV_FILE"
}
trap cleanup EXIT
"${COMPOSE[@]}" up -d --no-build --pull never
"${COMPOSE[@]}" exec -T admin python - <<'PY'
import time
import urllib.request
for attempt in range(60):
    try:
        urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=1).close()
        break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit('admin did not start')
PY
# 空模型阶段 bot 应保持等待，不能进入重启循环。
"${COMPOSE[@]}" logs bot | grep 'Waiting for an active model'
"${COMPOSE[@]}" exec -T admin python < "$ROOT/tests/container_smoke.py"
"${COMPOSE[@]}" exec -T bot python bot/main.py --check-onebot
"${COMPOSE[@]}" exec -T bot python bot/main.py --check-dsh
"${COMPOSE[@]}" exec -T bot sh -c 'id; node -e '\''require("node:sqlite")'\''; test -w /etc/qqbot/config.toml; printf persisted > /var/lib/qqbot/video-media/outbox/smoke-marker'
"${COMPOSE[@]}" restart bot admin
"${COMPOSE[@]}" exec -T bot sh -c 'test "$(cat /var/lib/qqbot/video-media/outbox/smoke-marker)" = persisted; python bot/main.py --list-models'
"${COMPOSE[@]}" exec -T napcat sh -c 'test "$(cat /app/qqbot-video/smoke-marker)" = persisted; test ! -w /app/qqbot-video'
"${COMPOSE[@]}" run --rm --no-deps napcat-init
"${COMPOSE[@]}" exec -T admin python - <<'PY'
import urllib.request
assert urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status == 200
print('RESTART_HEALTH_OK')
PY
