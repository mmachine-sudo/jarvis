#!/bin/bash
# Печатает секреты Jarvis строками KEY='value' (для eval). Только root. Значения нигде не логируются.
# Источник — Infisical через /usr/local/bin/mm-secrets (проект mmachine, prod: доступ сервера уже настроен).
# Имена MMachine переводятся в имена Jarvis; id владельца — /etc/jarvis/owner_id (перенесён из MMachine).
set -euo pipefail
eval "$(/usr/local/bin/mm-secrets)"
q() { printf "%s='%s'\n" "$1" "${2//\'/}"; }
q OPENROUTER_API_KEY "${JARVIS_OPENROUTER_API_KEY:-${OPENROUTER_API_KEY:-}}"
q TELEGRAM_BOT_TOKEN "${JARVIS_TELEGRAM_BOT_TOKEN:-${MM_TELEGRAM_BOT_TOKEN:-}}"
OWNER="${JARVIS_TELEGRAM_OWNER_ID:-$(cat /etc/jarvis/owner_id 2>/dev/null || true)}"
q TELEGRAM_OWNER_ID "$OWNER"
q NOTION_TOKEN "${NOTION_TOKEN:-}"
