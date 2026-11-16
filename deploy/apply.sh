#!/bin/bash
# Применение текущего коммита /opt/jarvis на сервере (root). Вызывает deploy.sh; ненулевой выход → откат.
# Последняя строка вывода — статус для журнала.
export LC_ALL=C.UTF-8
set -euo pipefail
APP=/opt/jarvis VENV=/opt/jarvis-venv DATA=/var/lib/jarvis
id jarvis >/dev/null 2>&1 || useradd --system --home $DATA --shell /usr/sbin/nologin jarvis
install -d -o jarvis -g jarvis -m 700 $DATA
install -d -o root -g jarvis -m 750 /etc/jarvis
chmod +x $APP/deploy/*.sh

# окружение Python (вне репозитория — git reset его не трогает)
dpkg -s python3-venv >/dev/null 2>&1 || apt-get install -y -q python3-venv >/dev/null
[ -x $VENV/bin/python ] || python3 -m venv $VENV
REQ=$(sha256sum $APP/pyproject.toml | cut -c1-16)
[ "$(cat $VENV/.req 2>/dev/null)" = "$REQ" ] || { $VENV/bin/pip install -q -e "$APP[dev]" && echo "$REQ" > $VENV/.req; }

# тесты: не прошли — не перезапускаем
(cd $APP && $VENV/bin/python -m pytest -q -p no:cacheprovider) >/tmp/jarvis-tests.log 2>&1 \
  || { tail -n 15 /tmp/jarvis-tests.log; echo "jarvis: ТЕСТЫ НЕ ПРОШЛИ"; exit 1; }

# конфиг живёт вне репозитория: создаётся из примера один раз, правки владельца сохраняются
if [ ! -f /etc/jarvis/jarvis.yaml ]; then
  sed 's#^db_path:.*#db_path: /var/lib/jarvis/jarvis.db#' $APP/config/jarvis.example.yaml > /etc/jarvis/jarvis.yaml
fi
chown root:jarvis /etc/jarvis/jarvis.yaml; chmod 640 /etc/jarvis/jarvis.yaml

# SearXNG (свой поиск, только 127.0.0.1:8888) — переехал из MMachine
SX=/var/lib/jarvis-searxng; install -d -m 777 $SX
[ -f /etc/jarvis/searxng.secret ] || { openssl rand -hex 32 > /etc/jarvis/searxng.secret; chmod 600 /etc/jarvis/searxng.secret; }
sed "s/__SECRET__/$(cat /etc/jarvis/searxng.secret)/" $APP/deploy/searxng/settings.yml > $SX/settings.yml.new
if ! cmp -s $SX/settings.yml.new $SX/settings.yml || [ "$(docker inspect -f '{{.State.Running}}' searxng 2>/dev/null)" != "true" ]; then
  mv $SX/settings.yml.new $SX/settings.yml
  docker rm -f searxng >/dev/null 2>&1 || true
  docker run -d --name searxng --restart unless-stopped -p 127.0.0.1:8888:8080 -v $SX:/etc/searxng searxng/searxng:latest >/dev/null || true
  for i in $(seq 1 20); do curl -fs http://127.0.0.1:8888/healthz >/dev/null 2>&1 && break; sleep 3; done
else
  rm -f $SX/settings.yml.new
fi
SXN=$(curl -s -m 30 "http://127.0.0.1:8888/search?q=thailand+visa&format=json" | jq '.results|length' 2>/dev/null || echo 0)

# службы
install -m 644 $APP/deploy/systemd/*.service $APP/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q jarvis jarvis-deploy.timer jarvis-secrets-watch.timer
systemctl start jarvis-deploy.timer jarvis-secrets-watch.timer
/opt/jarvis/deploy/secrets.sh | sha256sum | cut -c1-32 > $DATA/.secrets.hash

# проверка перед запуском: выводит только «есть/нет» и id моделей, без значений секретов
DOCALL=$($APP/deploy/run.sh doctor 2>&1 || true)
DOC=$(echo "$DOCALL" | grep -E '✗|!|Error|error' | head -6 | tr '\n' ' ' | cut -c1-500 || true)
systemctl restart jarvis; sleep 12
systemctl is-active --quiet jarvis || { echo "doctor: $(echo "$DOCALL" | tail -n 8 | tr '\n' ' ' | cut -c1-400)"; journalctl -u jarvis -n 40 --no-pager -o cat | grep -v '^ *$' | tail -n 8; echo "jarvis: НЕ ЗАПУСТИЛСЯ · doctor: ${DOC:-ок}"; exit 1; }
git -C $APP rev-parse --short HEAD > $DATA/.deployed-commit
echo "jarvis: работает · поиск SearXNG ${SXN:-0} · doctor: ${DOC:-всё ок}"
