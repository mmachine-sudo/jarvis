#!/bin/bash
# Каждые 3 мин (root): если в Infisical изменились секреты — перезапустить Jarvis.
set -uo pipefail
H=$(/opt/jarvis/deploy/secrets.sh | sha256sum | cut -c1-32) || exit 0
F=/var/lib/jarvis/.secrets.hash
if [ "$(cat $F 2>/dev/null)" != "$H" ]; then
  echo "$H" > $F; chmod 600 $F
  systemctl restart jarvis && /opt/jarvis/deploy/notify.sh "секреты изменились — Jarvis перезапущен"
fi
