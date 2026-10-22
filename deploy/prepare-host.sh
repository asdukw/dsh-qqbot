#!/usr/bin/env bash
# Prepare an Ubuntu/Debian host for NapCat + the bot.
# Usage: sudo bash deploy/prepare-host.sh [--swap] [--mirror https://your.mirror]
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root" >&2
    exit 1
fi

use_swap=0
mirror=""
while [ $# -gt 0 ]; do
    case "$1" in
        --swap) use_swap=1 ;;
        --mirror)
            shift
            mirror="${1:?--mirror requires a URL}"
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
    shift
done

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 python3-venv openssl curl ca-certificates
systemctl enable --now docker >/dev/null
if ! docker compose version >/dev/null 2>&1; then
    echo "Docker Compose v2 is required (the 'docker compose' command)." >&2
    exit 1
fi

if [ -n "$mirror" ]; then
    if [ -f /etc/docker/daemon.json ]; then
        echo "warning: /etc/docker/daemon.json already exists; not overwriting it." >&2
        echo "add the mirror manually if needed: $mirror" >&2
    else
        install -d -m 755 /etc/docker
        cat > /etc/docker/daemon.json <<EOF
{
  "registry-mirrors": ["$mirror"]
}
EOF
        systemctl restart docker
    fi
fi

if [ "$use_swap" = 1 ]; then
    if swapon --show | grep -q '/swapfile'; then
        echo "swap already active"
    else
        fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
        swapon /swapfile
        grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
        echo 'vm.swappiness=10' > /etc/sysctl.d/99-qqbot-swap.conf
        sysctl -q -p /etc/sysctl.d/99-qqbot-swap.conf
        echo "2G swap enabled"
    fi
fi

echo
echo "host ready. next:"
echo "  sudo bash deploy/napcat/setup-napcat.sh"
echo "  sudo napcat-qr"
