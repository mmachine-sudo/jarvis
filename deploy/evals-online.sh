#!/bin/bash
# Онлайн-прогон эталонов на настоящих моделях (тратит деньги — только по согласию владельца).
# Запускается выкладкой, когда меняется ops/evals-online.id. Итог — строками в Notion.
export LC_ALL=C.UTF-8
OUT=$(/opt/jarvis/deploy/run.sh evals --online --cases /opt/jarvis/evals/cases.yaml --max-usd 3 2>&1)
/opt/jarvis/deploy/notify.sh "эталоны онлайн (запрос $(cat /opt/jarvis/ops/evals-online.id)):"
echo "$OUT" | grep -E '^(✓|✗|■|Прошло)' | while IFS= read -r l; do /opt/jarvis/deploy/notify.sh "$l"; done
echo "$OUT" | grep -qE '^Прошло' || /opt/jarvis/deploy/notify.sh "эталоны: прогон упал · $(echo "$OUT" | grep -v '^\s*$' | tail -n 6 | tr '\n' ' ' | cut -c1-700)"
