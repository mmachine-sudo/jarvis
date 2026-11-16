"""Сборка приложения и главный цикл: бот + воркер + планировщик."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Settings
from .gateway import ModelGateway
from .guard import Guard
from .journal import Journal
from .memory import Memory
from .orchestrator import Orchestrator
from .reader import QuarantineReader, Searcher
from .store import Store
from .telegram import TelegramBot
from .treasury import Treasury

log = logging.getLogger("jarvis")


@dataclass
class App:
    settings: Settings
    store: Store
    treasury: Treasury
    gateway: ModelGateway
    memory: Memory
    journal: Journal
    guard: Guard
    bot: TelegramBot
    orch: Orchestrator


def build(settings: Settings) -> App:
    if not (settings.openrouter_api_key and settings.telegram_bot_token and settings.telegram_owner_id):
        raise SystemExit("Нет секретов в окружении: OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID. "
                         "Запусти через Infisical (см. README) и проверь `jarvis doctor`.")
    store = Store(settings.db_path)
    treasury = Treasury(store, settings.budget, settings.owner.timezone)
    gateway = ModelGateway(settings, treasury, store)
    memory = Memory(store)
    memory.seed_profile(settings.owner.profile)
    journal = Journal(store)
    guard = Guard(store)
    bot = TelegramBot(settings.telegram_bot_token, settings.telegram_owner_id, store)
    reader = QuarantineReader(settings, gateway)
    searcher = Searcher(settings.searxng_url) if settings.searxng_url else None
    orch = Orchestrator(settings, store, gateway, memory, journal, guard, bot, reader, searcher)
    bot.orch, bot.memory, bot.journal, bot.treasury, bot.guard, bot.gateway = orch, memory, journal, treasury, guard, gateway
    return App(settings, store, treasury, gateway, memory, journal, guard, bot, orch)


async def worker(app: App, interval: float = 2.0) -> None:
    while True:
        try:
            await app.orch.run_pending()
        except Exception:
            log.exception("worker tick failed")
        await asyncio.sleep(interval)


async def scheduler(app: App, interval: float = 60.0) -> None:
    tz = ZoneInfo(app.settings.owner.timezone)
    while True:
        try:
            await tick_schedule(app, datetime.now(tz))
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(interval)


async def tick_schedule(app: App, now: datetime) -> None:
    for e in app.journal.due(now.date().isoformat()):
        app.journal.mark_asked(e["id"])
        await app.bot.notify(f"📓 Проверка прогноза #{e['id']} ({e['task_id']}):\n«{e['question']}»\n"
                             f"Jarvis давал p = {e['p']:.2f}. Сбылось?",
                             [("Да, сбылось", f"jr:{e['id']}:1"), ("Нет", f"jr:{e['id']}:0")])
    day = now.date().isoformat()
    if now.hour == 8 and now.minute >= 30 and app.store.get("summary_day") != day:
        app.store.set("summary_day", day)
        await app.bot.notify("☀️ Утренняя сводка\n" + app.bot.status_text())


async def run(settings: Settings) -> None:
    app = build(settings)
    log.info("Jarvis запущен")
    await app.bot.notify("Jarvis запущен. /help — команды.")
    await asyncio.gather(app.bot.poll_forever(), worker(app), scheduler(app))
