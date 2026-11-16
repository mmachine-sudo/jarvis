#!/usr/bin/env bash
# Запускает команду с секретами из Infisical (machine identity, universal auth).
# Файл /etc/jarvis/infisical.env (права 600, владелец root или jarvis) содержит:
#   INFISICAL_CLIENT_ID=...
#   INFISICAL_CLIENT_SECRET=...
#   INFISICAL_PROJECT_ID=...
#   INFISICAL_ENV=prod
# Сами ключи OpenRouter/Telegram хранятся только в Infisical.
set -euo pipefail

ENV_FILE="${INFISICAL_ENV_FILE:-/etc/jarvis/infisical.env}"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "Нет файла $ENV_FILE — см. README, раздел «Секреты»" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

INFISICAL_TOKEN="$(infisical login --method=universal-auth \
  --client-id="$INFISICAL_CLIENT_ID" --client-secret="$INFISICAL_CLIENT_SECRET" --silent --plain)"
export INFISICAL_TOKEN

exec infisical run --projectId="$INFISICAL_PROJECT_ID" --env="${INFISICAL_ENV:-prod}" -- "$@"
