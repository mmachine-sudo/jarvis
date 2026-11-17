#!/bin/bash
# Ежедневный бэкап Jarvis в Backblaze B2 через restic (те же ключи B2, что были у MMachine; отдельная папка jarvis).
# Хранение 30 дней. Последняя строка — статус. Значения секретов не печатаются.
export LC_ALL=C.UTF-8
set -uo pipefail
umask 077
STAGE=/var/lib/jarvis/backup; install -d -m 700 $STAGE
eval "$(/usr/local/bin/mm-secrets)" 2>/dev/null || { echo "бэкап: нет доступа к секретам"; exit 1; }
# имена переменных у MMachine могли быть любыми из распространённых — берём первое заданное
export B2_ACCOUNT_ID="${B2_ACCOUNT_ID:-${B2_KEY_ID:-${B2_APPLICATION_KEY_ID:-}}}"
export B2_ACCOUNT_KEY="${B2_ACCOUNT_KEY:-${B2_APPLICATION_KEY:-${B2_APP_KEY:-}}}"
export RESTIC_PASSWORD="${RESTIC_PASSWORD:-}"
BUCKET="${B2_BUCKET:-${B2_BUCKET_NAME:-}}"
if [ -z "$BUCKET" ] && [ -z "${RESTIC_REPOSITORY:-}" ]; then   # как у MMachine: бакет из её скрипта бэкапа
  S=$(systemctl cat mm-backup.service 2>/dev/null | sed -n 's/^ExecStart=//p' | awk '{print $1}')
  [ -n "$S" ] && [ -f "$S" ] && BUCKET=$(grep -oE 'b2:[A-Za-z0-9._-]+' "$S" | head -1 | cut -d: -f2)
fi
export RESTIC_REPOSITORY="b2:${BUCKET}:jarvis"
MISS=""; for v in B2_ACCOUNT_ID B2_ACCOUNT_KEY RESTIC_PASSWORD; do [ -n "${!v}" ] || MISS="$MISS $v"; done
[ -n "$BUCKET" ] || MISS="$MISS бакет"
if [ -n "$MISS" ]; then
  echo "бэкап: не хватает:$MISS · имена в Infisical: $(/usr/local/bin/mm-secrets --names 2>/dev/null | grep -iE 'b2|restic' | tr '\n' ' ')"; exit 1
fi
python3 - <<'PY' || { echo "бэкап: снимок базы не удался"; exit 1; }
import sqlite3
s = sqlite3.connect("/var/lib/jarvis/jarvis.db"); d = sqlite3.connect("/var/lib/jarvis/backup/jarvis.db")
s.backup(d); d.close(); s.close()
PY
restic cat config >/dev/null 2>&1 || restic init >/dev/null 2>&1 || { echo "бэкап: restic init не прошёл"; exit 1; }
OUT=$(restic backup -q $STAGE /etc/jarvis /var/lib/jarvis/inherit 2>&1) || { echo "бэкап: ошибка restic: $(echo "$OUT" | tail -2 | tr '\n' ' ' | cut -c1-200)"; exit 1; }
restic forget -q --keep-daily 30 --prune >/dev/null 2>&1 || true
date -u +%FT%TZ > /var/lib/jarvis/.last-backup; chmod 644 /var/lib/jarvis/.last-backup
echo "бэкап: ок $(cat /var/lib/jarvis/.last-backup)"
