#!/bin/bash
# Строка в журнал Notion «Jarvis — сервер» (страница бывшего журнала MMachine): notify.sh "текст".
PAGE=4062c835-ac77-81f8-ab77-d7f16258259d
LINE="$(date -u +'%Y-%m-%d %H:%M UTC') · $1"
( eval "$(/opt/jarvis/deploy/secrets.sh)"
  jq -n --arg l "$LINE" '{children:[{object:"block",type:"bulleted_list_item",bulleted_list_item:{rich_text:[{type:"text",text:{content:$l}}]}}]}' \
  | curl -fsS -X PATCH "https://api.notion.com/v1/blocks/$PAGE/children" -H "Authorization: Bearer $NOTION_TOKEN" \
    -H "Notion-Version: 2022-06-28" -H 'Content-Type: application/json' --data @- >/dev/null
  if [ -n "${2:-}" ]; then
    jq -n --arg t "$2" '{properties:{title:{title:[{text:{content:$t}}]}}}' \
    | curl -fsS -X PATCH "https://api.notion.com/v1/pages/$PAGE" -H "Authorization: Bearer $NOTION_TOKEN" \
      -H "Notion-Version: 2022-06-28" -H 'Content-Type: application/json' --data @- >/dev/null
  fi ) || true
