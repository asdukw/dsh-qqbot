#!/usr/bin/env bash
# Print the newest NapCat login QR code from container logs.
#   napcat-qr           # newest QR found in logs
#   napcat-qr --fresh   # restart NapCat to force a brand-new QR, then print it
set -euo pipefail

BASE=${NAPCAT_DIR:-/opt/qqbot/napcat}
cd "$BASE"

fresh=0
case "${1:-}" in
    -f|--fresh) fresh=1 ;;
    "") ;;
    *) echo "usage: napcat-qr [--fresh]" >&2; exit 2 ;;
esac

if [ "$fresh" = 1 ]; then
    docker compose restart napcat >/dev/null
    sleep 14
fi

logs=$(docker compose logs --tail=600 napcat 2>&1 | sed 's/^napcat  | //')

block=$(printf '%s\n' "$logs" | awk '
    /▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄/ { buf=""; inb=1 }
    inb { buf = buf $0 "\n" }
    /二维码解码URL/ { if (inb) { last=buf; inb=0 } }
    END { printf "%s", last }
')

if [ -z "$block" ]; then
    echo "最近日志里没有二维码。试试：napcat-qr --fresh" >&2
    exit 1
fi

printf '%s\n' "$block"

url=$(printf '%s\n' "$logs" | grep -oE 'https://txz\.qq\.com/p\?[^ ]+' | tail -n1 || true)
if [ -n "$url" ]; then
    printf '\n解码地址（手机浏览器也可直接打开）：\n%s\n' "$url"
fi

account=$(ls data/config/onebot11_*.json 2>/dev/null | head -n1 || true)
if [ -n "$account" ]; then
    printf '\n已检测到账号配置：%s\n' "$(basename "$account")"
fi

if [ -f data/config/webui.json ]; then
    token=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("token",""))' data/config/webui.json 2>/dev/null || true)
    if [ -n "$token" ]; then
        printf 'WebUI（仅服务器本机）：http://127.0.0.1:6099/webui?token=%s\n' "$token"
    fi
fi
