"""Сквозной прогон с настоящим шлюзом: подставной OpenRouter отвечает по системному промпту роли.

Проверяет то, чего не видят тесты со ScriptedGateway: промпты собираются без ошибок
форматирования, JSON проходит через настоящий разбор, критик уходит к другому вендору,
стоимость каждого вызова попадает в казну и в ход работы.
"""
from __future__ import annotations

import json

import httpx

from jarvis.config import Settings
from jarvis.gateway import ModelGateway, vendor_of
from jarvis.guard import Guard
from jarvis.journal import Journal
from jarvis.memory import Memory
from jarvis.orchestrator import Orchestrator
from jarvis.store import Store
from jarvis.treasury import Treasury

from .conftest import FINDING, PLAN, CollectNotifier, card, result, verdict


def openrouter_mock(log: list[dict]):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        system = body["messages"][0]["content"]
        log.append({"model": body["model"], "system": system[:60], "user": body["messages"][1]["content"]})
        if "карточку задачи" in system:
            data = card("B")
        elif "Разбей задачу" in system:
            data = PLAN
        elif "субагент-исследователь" in system:
            data = FINDING
        elif "независимый проверяющий" in system:
            data = verdict(True, "minor", ["Можно добавить сроки"])
        else:  # solo / synth
            data = result()
        content = "Вот ответ:\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"cost": 0.002}})
    return handler


async def test_end_to_end_team_with_real_gateway():
    s = Settings()
    s.openrouter_api_key = "test"
    s.owner.rules = ["Правило {с фигурными скобками} не ломает форматирование"]
    store = Store()
    treasury = Treasury(store, s.budget)
    log: list[dict] = []
    gw = ModelGateway(s, treasury, store, transport=httpx.MockTransport(openrouter_mock(log)))
    note = CollectNotifier()
    orch = Orchestrator(s, store, gw, Memory(store), Journal(store), Guard(store), note)
    tid = orch.submit("Сравни три варианта VPS для Jarvis, мой email test@mail.ru")
    for _ in range(5):
        await orch.run_pending()
    t = store.task(tid)
    assert t["status"] == "done", t
    # обезличивание: email не ушёл в облако
    assert all("test@mail.ru" not in x["user"] for x in log)
    # критик — другой вендор, чем автор сводки
    synth_model = [x for x in log if "оркестратор Jarvis. Сведи" in x["system"]][0]["model"]
    critic_model = [x for x in log if "независимый проверяющий" in x["system"]][0]["model"]
    assert vendor_of(critic_model) != vendor_of(synth_model)
    # каждый вызов оплачен и виден в ходе работы
    assert treasury.spent_task(tid) == len(log) * 0.002
    assert "Мелкие замечания" in note.last[0]
    await gw.aclose()
