#!/bin/sh
set -eu
umask 077
DATA=${QQBOT_DATA_DIR:-/var/lib/qqbot}
mkdir -p "$DATA" "$DATA/dsh-home" "$DATA/dsh-workspace/spill" "$DATA/dsh-state" "$DATA/video-media/outbox" "$DATA/video-media/staging"
if [ -z "${ADMIN_UI_PASSWORD:-}" ]; then echo 'ADMIN_UI_PASSWORD must be set; refusing to start' >&2; exit 64; fi
case "${QQBOT_ROLE:-bot}" in
  bot|admin) ;;
  *) echo "unknown QQBOT_ROLE=${QQBOT_ROLE:-}" >&2; exit 64 ;;
esac
# 转发信号到初始化进程，避免等待模型时 docker stop 超时。
python /opt/qqbot/deploy/container_bootstrap.py &
bootstrap=$!
trap 'kill -TERM "$bootstrap" 2>/dev/null || true; wait "$bootstrap" || true; exit 0' TERM INT
wait "$bootstrap"
trap - TERM INT
if [ "${QQBOT_ROLE:-bot}" = admin ]; then
  exec python /opt/qqbot/bot/admin_ui.py --config /etc/qqbot/config.toml
fi
exec python /opt/qqbot/bot/main.py --config /etc/qqbot/config.toml
