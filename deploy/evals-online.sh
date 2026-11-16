#!/bin/bash
# Онлайн-прогон эталонов на настоящих моделях (тратит деньги — только по согласию владельца).
# Запускается выкладкой, когда меняется ops/evals-online.id. Каждая строка итога — сразу в Notion.
export LC_ALL=C.UTF-8
/opt/jarvis/deploy/notify.sh "эталоны онлайн: старт (запрос: $(cat /opt/jarvis/ops/evals-online.id))"
LOG=/var/log/jarvis-evals.log; : > $LOG
/opt/jarvis/deploy/run.sh evals --online --cases /opt/jarvis/evals/cases.yaml --max-usd 3 2>>$LOG \
  | while IFS= read -r l; do
      echo "$l" >> $LOG
      case "$l" in ✓*|✗*|■*|Прошло*) /opt/jarvis/deploy/notify.sh "эталон $l";; esac
    done
grep -q '^Прошло' $LOG || /opt/jarvis/deploy/notify.sh "эталоны: прогон прерван · $(grep -v '^\s*$' $LOG | tail -n 6 | tr '\n' ' ' | cut -c1-700)"
