"""Учёт доработок и ежедневный разбор их причин (перенесено из MMachine: mm/rework.py + audit.rework_review,
переписано под Jarvis).

Каждый круг доработки пишется в таблицу rework: «место» (режим|класс задачи), раунд, сколько стоил круг
(это и есть потеря) и замечания. Раз в сутки Jarvis смотрит, где доработки повторяются, и одной дешёвой моделью
сводит причины в 1–3 правила. Правила не применяются сами: они становятся кандидатами в плейбук и идут через
ворота владельца (/playbook, /promote) — так решено в v4. Владелец видит в сводке, сколько денег ушло на доработки.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

from .gateway import Gateway, as_messages
from .memory import Memory
from .store import Store, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS rework (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, task_id TEXT, place TEXT, round INTEGER,
  usd REAL DEFAULT 0, issues TEXT
);
CREATE INDEX IF NOT EXISTS rework_ts ON rework(ts);
"""

REVIEW_SYSTEM = """Ты разбираешь причины, по которым результаты Jarvis возвращались на доработку.
Дано: место (режим и класс задачи) и замечания проверяющих по нескольким кругам.
Найди повторяющиеся причины и сформулируй до 3 коротких правил для исполнителя, которые предотвратят такие
возвраты. Правило — одна фраза, конкретная и проверяемая. Если причина — придирки проверяющего сверх карточки,
так и напиши правилом для проверяющего, начав с «Проверяющему:». Не выдумывай причин, которых нет в замечаниях.
Верни ТОЛЬКО JSON: {"rules": [str]}"""


class ReworkRules(BaseModel):
    rules: list[str] = Field(default_factory=list)


def place_of(mode: str, cls: str) -> str:
    return f"{mode}|{cls}"


class Rework:
    def __init__(self, store: Store):
        self.store = store
        store.conn.executescript(SCHEMA)

    def log_round(self, task_id: str, place: str, rnd: int, issues: list[str]) -> float:
        """Записать круг доработки. Стоимость круга = траты задачи, ещё не отнесённые к прошлым кругам."""
        spent = self.store.one("SELECT COALESCE(SUM(cost_usd),0) s FROM steps WHERE task_id=?", (task_id,))["s"]
        counted = self.store.one("SELECT COALESCE(SUM(usd),0) s FROM rework WHERE task_id=?", (task_id,))["s"]
        usd = max(float(spent) - float(counted), 0.0)
        self.store.x("INSERT INTO rework(ts,task_id,place,round,usd,issues) VALUES(?,?,?,?,?,?)",
                     (now_iso(), task_id, place, rnd, round(usd, 5), json.dumps(issues[:6], ensure_ascii=False)))
        return usd

    def stats(self, days: int = 7) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        tot = self.store.one("SELECT COUNT(*) n, COALESCE(SUM(usd),0) usd FROM rework WHERE ts>=?", (since,))
        spent = self.store.one("SELECT COALESCE(SUM(usd),0) s FROM spend WHERE ts>=?", (since,))["s"] or 0
        by = self.store.all("SELECT place, COUNT(*) n, ROUND(SUM(usd),4) usd FROM rework WHERE ts>=? "
                            "GROUP BY place ORDER BY usd DESC, n DESC", (since,))
        return {"n": tot["n"], "usd": round(tot["usd"] or 0, 4), "spent": round(spent, 4),
                "share": round((tot["usd"] or 0) / spent, 3) if spent else None, "by_place": by, "since": since}

    def samples(self, place: str, since: str, limit: int = 8) -> list[str]:
        rows = self.store.all("SELECT round, usd, issues FROM rework WHERE place=? AND ts>=? ORDER BY id DESC LIMIT ?",
                              (place, since, limit))
        return [f"круг {r['round']}, ${r['usd']:.3f}: " + "; ".join(Store.loads(r["issues"], []))[:500] for r in rows]

    async def daily_review(self, gw: Gateway, memory: Memory, *, min_rounds: int = 2,
                           usd_cap: float = 0.05) -> str | None:
        """Сводка для владельца или None, если разбирать нечего. Новые правила — кандидаты в плейбук."""
        st = self.stats(7)
        if not st["n"]:
            return None
        lines = [f"🔁 Доработки за 7 дней: {st['n']} кругов, ${st['usd']:.2f}"
                 + (f" ({st['share']:.0%} всех трат на модели)" if st["share"] is not None else "")]
        new_rules: list[str] = []
        for row in st["by_place"][:3]:
            lines.append(f"• {row['place']}: {row['n']} кругов, ${row['usd'] or 0:.2f}")
            if row["n"] < min_rounds:
                continue
            user = {"место": row["place"], "замечания": self.samples(row["place"], st["since"])}
            try:
                reply = await gw.chat("worker", as_messages(REVIEW_SYSTEM, user), schema=ReworkRules,
                                      task_cap=usd_cap, max_tokens=600, temperature=0.0)
            except Exception:  # noqa: BLE001 — разбор необязателен, сводка уйдёт и без правил
                continue
            for rule in reply.data.rules[:3]:
                rule = " ".join(str(rule).split())[:300]
                if len(rule) >= 8 and memory.propose_lesson(rule, f"разбор доработок: {row['place']}"):
                    new_rules.append(rule)
        if new_rules:
            lines.append("Новые кандидаты в плейбук (принять — /playbook, затем /promote N):")
            lines += [f"– {r}" for r in new_rules]
        return "\n".join(lines)
