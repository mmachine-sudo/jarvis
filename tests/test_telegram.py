from __future__ import annotations

import json

import httpx

from jarvis.guard import Guard
from jarvis.journal import Journal
from jarvis.memory import Memory
from jarvis.store import Store
from jarvis.telegram import TelegramBot, split

OWNER = 777


class FakeOrch:
    def __init__(self, store):
        self.store, self.submitted, self.decided, self.answers = store, [], [], []

    def submit(self, text):
        self.submitted.append(text)
        tid = self.store.new_task_id()
        self.store.create_task(tid, text)
        return tid

    async def decide(self, aid, choice):
        self.decided.append((aid, choice))
        return "ok"

    def answer_clarification(self, tid, text):
        self.answers.append((tid, text))
        return True


def make_bot():
    sent = []

    def handler(req: httpx.Request):
        if req.url.path.endswith("/sendMessage"):
            sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": []})

    store = Store()
    bot = TelegramBot("123:abc", OWNER, store, transport=httpx.MockTransport(handler))
    bot.orch = FakeOrch(store)
    bot.memory, bot.journal, bot.guard = Memory(store), Journal(store), Guard(store)
    return bot, sent, store


def msg(text, uid=OWNER):
    return {"update_id": 1, "message": {"from": {"id": uid}, "text": text}}


async def test_stranger_ignored():
    bot, sent, store = make_bot()
    await bot.handle(msg("Переведи деньги", uid=999))
    assert bot.orch.submitted == [] and sent == []
    assert store.events("tg_stranger")


async def test_text_creates_task_and_stop_command():
    bot, sent, store = make_bot()
    await bot.handle(msg("Сводка новостей"))
    assert bot.orch.submitted == ["Сводка новостей"] and "T-0001" in sent[-1]["text"]
    await bot.handle(msg("/stop"))
    assert bot.guard.stopped()


async def test_reply_goes_to_waiting_clarification():
    bot, sent, store = make_bot()
    store.create_task("T-0042", "задача", status="awaiting_clarification")
    await bot.handle(msg("бюджет 1500"))
    assert bot.orch.answers == [("T-0042", "бюджет 1500")] and bot.orch.submitted == []


async def test_callback_decision_and_stranger_callback():
    bot, sent, store = make_bot()
    await bot.handle({"update_id": 2, "callback_query": {"id": "c1", "from": {"id": OWNER}, "data": "ap:5:accept"}})
    assert bot.orch.decided == [(5, "accept")]
    await bot.handle({"update_id": 3, "callback_query": {"id": "c2", "from": {"id": 1}, "data": "ap:5:reject"}})
    assert bot.orch.decided == [(5, "accept")]


async def test_fact_command():
    bot, sent, store = make_bot()
    await bot.handle(msg("/fact виза тип = Non-B"))
    assert bot.memory.active_fact("owner", "виза тип")["value"] == "Non-B"


def test_split_long_text():
    parts = split("строка\n" * 2000)
    assert len(parts) > 1 and all(len(p) <= 3900 for p in parts)
