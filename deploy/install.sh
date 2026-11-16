#!/usr/bin/env bash
# Установка Jarvis на VPS (Ubuntu 24.04). Запускать от root из каталога /opt/jarvis
# после того, как код склонирован туда. Повторный запуск безопасен.
set -euo pipefail

APP=/opt/jarvis
DATA=/var/lib/jarvis

id jarvis &>/dev/null || useradd --system --home "$DATA" --shell /usr/sbin/nologin jarvis
mkdir -p "$DATA" /etc/jarvis
chown jarvis:jarvis "$DATA"
chmod 750 /etc/jarvis

command -v python3 >/dev/null || apt-get install -y python3 python3-venv
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q "$APP"

[[ -f "$APP/config/jarvis.yaml" ]] || cp "$APP/config/jarvis.example.yaml" "$APP/config/jarvis.yaml"
chmod +x "$APP/deploy/run-with-secrets.sh"

if ! command -v infisical >/dev/null; then
  echo "Infisical CLI не найден. Установи его (см. README) и запусти install.sh ещё раз." >&2
  exit 1
fi
if [[ ! -f /etc/jarvis/infisical.env ]]; then
  echo "Нет /etc/jarvis/infisical.env — создай его по README (раздел «Секреты»)." >&2
  exit 1
fi
chown root:jarvis /etc/jarvis/infisical.env
chmod 640 /etc/jarvis/infisical.env

echo "Проверка перед запуском:"
sudo -u jarvis env JARVIS_CONFIG="$APP/config/jarvis.yaml" JARVIS_DB="$DATA/jarvis.db" \
  "$APP/deploy/run-with-secrets.sh" "$APP/.venv/bin/python" -m jarvis doctor || {
  echo "doctor нашёл проблемы — исправь и запусти install.sh снова." >&2
  exit 1
}

cp "$APP/deploy/jarvis.service" /etc/systemd/system/jarvis.service
systemctl daemon-reload
systemctl enable --now jarvis
systemctl --no-pager status jarvis | head -5
echo "Готово. Напиши боту /help в Telegram."
