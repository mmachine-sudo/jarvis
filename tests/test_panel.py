"""Мост панели через Notion: снимок без секретов, команды из базы выполняются и получают ответ."""
from __future__ import annotations

import json

import httpx

from jarvis.app import build
from jarvis.config import Settings
from jarvis.panel import NotionPanel, decode, encode, panel_loop, run_command, snapshot


def _app(tmp_path):
    s = Settings()
    s.openrouter_api_key, s.telegram_bot_token, s.telegram_owner_id = "sk-or-v1-" + "a" * 40, "1:t", 42
    s.db_path = str(tmp_path / "j.db")
    return build(s)


def test_encode_roundtrip_and_chunks():
    data = {"x": "привет " * 2000}
    chunks = encode(data)
    assert all(len(c) <= 1900 for c in chunks) and decode(chunks) == data


async def test_snapshot_has_tasks_and_no_secrets(tmp_path):
    app = _app(tmp_path)
    tid = app.orch.submit("Сравни три VPS, ключ sk-or-v1-" + "b" * 40)
    snap = snapshot(app)
    assert snap["tasks"][0]["id"] == tid and "budget" in snap and "memory" in snap
    text = json.dumps(snap, ensure_ascii=False)
    assert "sk-or-v1-bbbb" not in text and "sk-or-v1-aaaa" not in text


async def test_commands_executed_and_answered(tmp_path):
    app = _app(tmp_path)
    answered, published = [], []
    rows = [
        {"id": "p1", "properties": {"Тип": {"select": {"name": "task"}}, "Команда": {"title": [{"plain_text": "Новая задача"}]},
                                     "Аргумент": {"rich_text": [{"plain_text": "Сводка новостей про AI"}]}}},
        {"id": "p2", "properties": {"Тип": {"select": {"name": "stop"}}, "Команда": {"title": [{"plain_text": "Стоп"}]},
                                     "Аргумент": {"rich_text": []}}},
        {"id": "p3", "properties": {"Тип": {"select": {"name": "fact"}}, "Команда": {"title": [{"plain_text": "Факт"}]},
                                     "Аргумент": {"rich_text": [{"plain_text": "город = Ча-ам"}]}}},
    ]

    def notion(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/query"):
            out, rows[:] = list(rows), []
            return httpx.Response(200, json={"results": out})
        if req.method == "PATCH" and "/pages/" in p:
            answered.append((p.rsplit("/", 1)[-1], json.loads(req.content)))
            return httpx.Response(200, json={})
        if req.method == "GET" and p.endswith("/children"):
            return httpx.Response(200, json={"results": [{"id": "code1", "type": "code"}]})
        if req.method == "PATCH" and p.endswith("/blocks/code1"):
            published.append(json.loads(req.content))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    panel = NotionPanel("t", "page", "db", client=httpx.AsyncClient(transport=httpx.MockTransport(notion)))
    import asyncio
    task = asyncio.create_task(panel_loop(app, panel, period=60, poll=0.01))
    await asyncio.sleep(0.15)
    task.cancel()
    assert [a[0] for a in answered] == ["p1", "p2", "p3"]
    assert all(a[1]["properties"]["Статус"]["select"]["name"] == "выполнена" for a in answered)
    assert app.store.task("T-0001")["request"] == "Сводка новостей про AI"
    assert app.guard.stopped() and app.memory.facts("город")
    rich = published[0]["code"]["rich_text"]
    snap = decode([r["text"]["content"] for r in rich])
    assert snap["stopped"] is True and snap["tasks"][0]["id"] == "T-0001"


async def test_unknown_command_is_error(tmp_path):
    app = _app(tmp_path)
    ok, text = await run_command(app, {"type": "rm", "arg": "", "title": "x"})
    assert not ok and "неизвестная" in text
