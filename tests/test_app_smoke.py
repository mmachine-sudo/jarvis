"""Дымовой тест всего приложения: Telegram → оркестратор → шлюз → ответ в Telegram."""
from __future__ import annotations

import json

import httpx

from jarvis.app import build, tick_schedule
from jarvis.config import Settings

from .test_integration import openrouter_mock


async def test_message_to_answer(tmp_path):
    s = Settings()
    s.openrouter_api_key, s.telegram_bot_token, s.telegram_owner_id = "k", "1:t", 42
    s.db_path = str(tmp_path / "j.db")
    app = build(s)
    sent: list[dict] = []

    def tg(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/sendMessage"):
            sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": []})

    calls: list[dict] = []
    app.bot.client = httpx.AsyncClient(transport=httpx.MockTransport(tg))
    app.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(openrouter_mock(calls)))

    await app.bot.handle({"update_id": 1, "message": {"from": {"id": 42}, "text": "Сравни три VPS для Jarvis"}})
    assert "T-0001 принята" in sent[-1]["text"]
    for _ in range(5):
        await app.orch.run_pending()
    assert app.store.task("T-0001")["status"] == "done"
    assert sent[-1]["text"].startswith("✅ T-0001")
    assert all(m["chat_id"] == 42 for m in sent)

    await app.bot.handle({"update_id": 2, "message": {"from": {"id": 42}, "text": "/task T-0001"}})
    assert "verify [critic" in sent[-1]["text"]

    # журнал: прогноз, срок которого наступил, приходит с кнопками
    app.journal.add("T-0001", "Сбудется?", 0.7, resolve_by="2000-01-01")
    from datetime import datetime
    await tick_schedule(app, datetime(2026, 11, 17, 9, 0))
    assert sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("jr:")
