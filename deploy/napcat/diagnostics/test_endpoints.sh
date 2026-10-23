#!/usr/bin/env bash
# Probe OpenCode Zen chat-completions endpoint variants without printing the key.
set -euo pipefail

set -a
# shellcheck disable=SC1091
. /etc/qqbot/bot.env
set +a

if [ -z "${OPENCODE_API_KEY:-}" ]; then
    echo "OPENCODE_API_KEY is empty" >&2
    exit 1
fi

bases=(
    "https://opencode.ai/zen/go"
    "https://opencode.ai/zen/go/v1"
)

for model in "deepseek-v4.1-flash-free" "deepseek-v4.1-flash"; do
    for base in "${bases[@]}"; do
        echo "=== ${model} @ ${base}/chat/completions ==="
        curl -sS --max-time 40 -w '\nHTTP %{http_code}\n' \
            "${base}/chat/completions" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer ${OPENCODE_API_KEY}" \
            -H "x-opencode-session: qqbot-diagnostics" \
            -d "{\"model\":\"${model}\",\"messages\":[{\"role\":\"user\",\"content\":\"只回复 ok\"}],\"max_tokens\":16}" \
            | head -c 600
        echo
    done
done
