"""Напоминания: «напомни …» — не исследование, а действие Jarvis. Дешёвая модель достаёт текст и время,
код хранит и в срок присылает сообщение в Telegram. Без критика: проверять нечего, ошибка видна владельцу сразу
(эталоны 16.11: критик отклонял «напомни купить воду», потому что у Jarvis не было способа напомнить)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .gateway import Gateway, as_messages
from .store import Store, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, text TEXT, due_utc TEXT, sent_at TEXT, created_at TEXT
);
"""

SYSTEM = """Из запроса владельца достань напоминание. Сейчас у владельца {now} ({tz}).
text — что напомнить, коротко, от второго лица («Купить воду»). when — локальное время владельца в формате
YYYY-MM-DD HH:MM. Если время не указано — завтра в 09:00; «вечером» — 19:00; «утром» — 09:00; «через N часов» — сейчас + N.
Верни ТОЛЬКО JSON: {{"text": str, "when": str}}"""


class Parsed(BaseModel):
    text: str
    when: str


class Reminders:
    def __init__(self, store: Store, tz: str):
        self.store, self.tz = store, ZoneInfo(tz)
        store.conn.executescript(SCHEMA)

    async def create(self, task_id: str, request: str, gw: Gateway, cap: float) -> tuple[str, datetime]:
        now = datetime.now(self.tz)
        system = SYSTEM.format(now=now.strftime("%Y-%m-%d %H:%M, %A"), tz=self.tz.key)
        reply = await gw.chat("solo_cheap", as_messages(system, request), task_id=task_id, schema=Parsed,
                              task_cap=cap, max_tokens=300, temperature=0.0)
        try:
            local = datetime.strptime(reply.data.when.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=self.tz)
        except ValueError:
            local = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        if local < now:
            local = now + timedelta(minutes=5)
        self.store.x("INSERT INTO reminders(task_id,text,due_utc,created_at) VALUES(?,?,?,?)",
                     (task_id, reply.data.text.strip()[:500], local.astimezone(timezone.utc).isoformat(timespec="seconds"),
                      now_iso()))
        return reply.data.text.strip(), local

    def due(self, now_utc: datetime | None = None) -> list[dict]:
        now_utc = now_utc or datetime.now(timezone.utc)
        return self.store.all("SELECT * FROM reminders WHERE sent_at IS NULL AND due_utc<=? ORDER BY due_utc",
                              (now_utc.isoformat(timespec="seconds"),))

    def mark_sent(self, rid: int) -> None:
        self.store.x("UPDATE reminders SET sent_at=? WHERE id=?", (now_iso(), rid))

    def pending(self) -> list[dict]:
        return self.store.all("SELECT * FROM reminders WHERE sent_at IS NULL ORDER BY due_utc")
