#!/usr/bin/env bash
# Install the bot on the target host. Run as root:  install-bot.sh <staging-dir>
set -euo pipefail

SRC="${1:?usage: install-bot.sh <staging-dir>}"
PIP_INDEX="https://mirrors.aliyun.com/pypi/simple/"

if ! getent group qqbot >/dev/null; then
    groupadd --system qqbot
fi
if ! id -u qqbot >/dev/null 2>&1; then
    useradd --system --gid qqbot --home-dir /opt/qqbot --shell /usr/sbin/nologin qqbot
fi

install -d -o root -g root -m 755 /opt/qqbot /opt/qqbot/bot
install -d -o qqbot -g qqbot -m 750 /var/lib/qqbot
install -d -o root -g qqbot -m 750 /etc/qqbot

cp -a "$SRC/bot/." /opt/qqbot/bot/
rm -rf /opt/qqbot/bot/__pycache__
install -d -o root -g qqbot -m 750 /opt/qqbot/cards
cp -a "$SRC/cards/." /opt/qqbot/cards/
chown -R root:qqbot /opt/qqbot/cards
chmod -R g+r /opt/qqbot/cards

python3 -m venv /opt/qqbot/bot/venv
/opt/qqbot/bot/venv/bin/pip install -q --upgrade pip -i "$PIP_INDEX"
/opt/qqbot/bot/venv/bin/pip install -q -r /opt/qqbot/bot/requirements.txt -i "$PIP_INDEX"
/opt/qqbot/bot/venv/bin/pip install -q 'deepseek-harness-sdk==0.2.1rc2'     -i https://pypi.tuna.tsinghua.edu.cn/simple     || /opt/qqbot/bot/venv/bin/pip install -q 'deepseek-harness-sdk==0.2.1rc2'

DSH_SDK=/opt/qqbot/bot/venv/bin/dsh
DSH_HOME=/var/lib/qqbot/dsh-home
install -d -o qqbot -g qqbot -m 750 "$DSH_HOME" /var/lib/qqbot/dsh-workspace /var/lib/qqbot/dsh-state /opt/qqbot/dsh/plugins
if [ ! -d "$DSH_HOME/profiles/qqbot" ]; then
    if command -v sudo >/dev/null 2>&1; then
        sudo -u qqbot env HOME=/var/lib/qqbot DSH_HOME="$DSH_HOME" "$DSH_SDK" --profile qqbot --from-default-profile sdk-minimal --dump-default-config >/dev/null
    else
        runuser -u qqbot -- env HOME=/var/lib/qqbot DSH_HOME="$DSH_HOME" "$DSH_SDK" --profile qqbot --from-default-profile sdk-minimal --dump-default-config >/dev/null
    fi
fi
install -m 644 "$SRC/dsh/qqbot.cordis.patch.yml" "$DSH_HOME/profiles/qqbot/cordis.patch.yml"
install -m 644 "$SRC/dsh/user-profile.mjs" /opt/qqbot/dsh/plugins/user-profile.mjs
chown -R qqbot:qqbot "$DSH_HOME" /opt/qqbot/dsh/plugins /var/lib/qqbot/dsh-workspace /var/lib/qqbot/dsh-state
install -m 755 "$SRC/deploy/napcat/napcat-qr.sh" /usr/local/bin/napcat-qr

if [ ! -f /etc/qqbot/config.toml ]; then
    install -m 640 -g qqbot /opt/qqbot/bot/config.example.toml /etc/qqbot/config.toml
fi

# If NapCat has already logged in, use the account number from its config.
account_file="$(ls /opt/qqbot/napcat/data/config/onebot11_*.json 2>/dev/null | head -n1 || true)"
if [ -n "$account_file" ]; then
    self_id="$(basename "$account_file")"
    self_id="${self_id#onebot11_}"
    self_id="${self_id%.json}"
    case "$self_id" in
        ''|*[!0-9]*) ;;
        *) sed -i "s/^self_id = .*/self_id = $self_id/" /etc/qqbot/config.toml || true ;;
    esac
fi
if [ ! -f /etc/qqbot/character.json ]; then
    install -m 640 -g qqbot /opt/qqbot/bot/character.example.json /etc/qqbot/character.json
fi

if [ ! -f /etc/qqbot/bot.env ]; then
    ONEBOT_TOKEN=""
    if [ -f /opt/qqbot/napcat/.env ]; then
        ONEBOT_TOKEN="$(sed -n 's/^ONEBOT_TOKEN=//p' /opt/qqbot/napcat/.env | head -n1)"
    fi
    umask 077
    printf 'ONEBOT_TOKEN=%s
' "$ONEBOT_TOKEN" > /etc/qqbot/bot.env
fi
python3 "$SRC/deploy/update-bot-env.py" /etc/qqbot/bot.env
chown root:qqbot /etc/qqbot/bot.env
chmod 640 /etc/qqbot/bot.env

install -m 644 "$SRC/deploy/qqbot.service" /etc/systemd/system/qqbot.service
systemctl daemon-reload
systemctl enable qqbot >/dev/null

echo "bot installed."
echo "next: put OPENCODE_API_KEY into /etc/qqbot/bot.env, then: systemctl start qqbot"
