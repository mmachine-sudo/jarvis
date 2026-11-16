#!/bin/bash
# Онлайн-прогон эталонов на настоящих моделях (тратит деньги — только по согласию владельца).
# Запускается выкладкой, когда меняется ops/evals-online.id. Каждая строка итога — сразу в Notion.
export LC_ALL=C.UTF-8
/opt/jarvis/deploy/notify.sh "эталоны онлайн: старт (запрос: $(cat /opt/jarvis/ops/evals-online.id))"
LOG=/var/log/jarvis-evals.log
# прошлый прогон завершён — его база уходит в архив, и его траты не входят в потолок нового (новое согласие)
grep -q '^Прошло' $LOG 2>/dev/null && for x in "" -wal -shm; do [ -f /var/lib/jarvis/evals.db$x ] && mv /var/lib/jarvis/evals.db$x /var/lib/jarvis/evals.prev.db$x; done
: > $LOG
CAP=$(grep -oE 'max_usd=[0-9.]+' /opt/jarvis/ops/evals-online.id | cut -d= -f2); CAP=${CAP:-3}
/opt/jarvis/deploy/run.sh evals --online --cases /opt/jarvis/evals/cases.yaml --max-usd $CAP 2>>$LOG \
  | while IFS= read -r l; do
      echo "$l" >> $LOG
      case "$l" in ✓*|✗*|■*|Прошло*) /opt/jarvis/deploy/notify.sh "эталон $l";; esac
    done
grep -q '^Прошло' $LOG || /opt/jarvis/deploy/notify.sh "эталоны: прогон прерван · $(grep -v '^\s*$' $LOG | tail -n 6 | tr '\n' ' ' | cut -c1-700)"
