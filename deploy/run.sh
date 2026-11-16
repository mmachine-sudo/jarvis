#!/bin/bash
# Запуск Jarvis (root → пользователь jarvis): секреты в окружение процесса, адреса SearXNG и локальной модели.
# Использование: run.sh run | doctor | status | evals
set -euo pipefail
set -a; eval "$(/opt/jarvis/deploy/secrets.sh)"; set +a
unset NOTION_TOKEN
export JARVIS_CONFIG=/etc/jarvis/jarvis.yaml JARVIS_DB=/var/lib/jarvis/jarvis.db HOME=/var/lib/jarvis PYTHONUNBUFFERED=1
curl -fs -m 3 http://127.0.0.1:8888/healthz >/dev/null 2>&1 && export JARVIS_SEARXNG_URL=http://127.0.0.1:8888
# Mac в сети Tailscale (как было у MMachine): первый онлайн-узел macOS → LM Studio на 1234
if command -v tailscale >/dev/null && tailscale status --json >/tmp/jarvis-ts.json 2>/dev/null; then
  MAC=$(jq -r '[.Peer[]? | select((.OS=="macOS") and .Online)][0].TailscaleIPs[0] // empty' /tmp/jarvis-ts.json)
  [ -n "$MAC" ] && export JARVIS_LOCAL_LLM_URL="http://$MAC:1234/v1"
fi
cd /opt/jarvis
exec setpriv --reuid=jarvis --regid=jarvis --init-groups /opt/jarvis-venv/bin/python -m jarvis "$@"
