#!/bin/bash
# GitOps (таймер jarvis-deploy, каждые 2 мин, root): новый коммит в main → apply.sh; сбой → откат на прошлый.
export LC_ALL=C.UTF-8
umask 022
set -uo pipefail
APP=/opt/jarvis LOG=/var/log/jarvis-deploy.log
exec 9>/run/jarvis-deploy.lock; flock -n 9 || exit 0
git -C $APP fetch -q origin main || exit 0
NEW=$(git -C $APP rev-parse --short origin/main)
OLD=$(cat /var/lib/jarvis/.deployed-commit 2>/dev/null || echo "")
if [ "${1:-}" != "--force" ]; then
  [ "$NEW" = "$OLD" ] && exit 0
  [ "$NEW" = "$(cat /var/lib/jarvis/.attempted-commit 2>/dev/null)" ] && exit 0   # этот коммит уже пробовали
fi
install -d -m 700 /var/lib/jarvis; echo "$NEW" > /var/lib/jarvis/.attempted-commit
git -C $APP reset -q --hard origin/main
# коммит меняет только ops/ (запросы разборов и проверок) — работающего Jarvis не перезапускаем
export ONLY_OPS=0
[ -n "$OLD" ] && git -C $APP cat-file -e "$OLD" 2>/dev/null && ! git -C $APP diff --name-only "$OLD" "$NEW" | grep -qv '^ops/' && ONLY_OPS=1
MSG=$(git -C $APP log -1 --format=%s | cut -c1-120)
OUT=$($APP/deploy/apply.sh 2>&1); RC=$?
echo "$(date -u +%FT%TZ) $NEW rc=$RC"$'\n'"$OUT" >> $LOG
LAST=$(echo "$OUT" | tail -n 1)
[ $RC -ne 0 ] && LAST="$LAST · $(echo "$OUT" | tail -n 12 | head -n 11 | tr '\n' ' ' | tr -d '\000-\010\013-\037' | cut -c1-900)"
if [ $RC -ne 0 ] && [ -n "$OLD" ]; then
  git -C $APP reset -q --hard "$OLD"
  $APP/deploy/apply.sh >> $LOG 2>&1 || true
  LAST="$LAST · ОТКАТ на $OLD"
fi
$APP/deploy/notify.sh "выкладка $NEW · $MSG · $LAST · диск $(df -h / | awk 'NR==2{print $5}') · память своб. $(free -m | awk '/Mem/{print $7}') МБ" \
  "Jarvis — сервер · $([ $RC -eq 0 ] && echo ок || echo СБОЙ) · $(date -u +'%Y-%m-%d %H:%M UTC') · $NEW"
# журнал в Notion — последние 40 строк (полная история в git и /var/log/jarvis-deploy.log)
( eval "$(/opt/jarvis/deploy/secrets.sh)"
  P=4062c835-ac77-81f8-ab77-d7f16258259d
  IDS=$(curl -fsS "https://api.notion.com/v1/blocks/$P/children?page_size=100" -H "Authorization: Bearer $NOTION_TOKEN" \
    -H "Notion-Version: 2022-06-28" | jq -r '[.results[] | select(.type=="bulleted_list_item") | .id] | .[:-40][]' 2>/dev/null)
  for b in $IDS; do curl -fsS -X DELETE "https://api.notion.com/v1/blocks/$b" -H "Authorization: Bearer $NOTION_TOKEN" \
    -H "Notion-Version: 2022-06-28" >/dev/null || true; done ) || true

