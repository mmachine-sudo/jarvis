"""Telegram-интерфейс: только для владельца, команды, кнопки решений, стоп."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from .journal import Journal
from .memory import Memory
from .store import Store

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from .treasury import Treasury
    from .guard import Guard
    from .gateway import ModelGateway

log = logging.getLogger("jarvis.telegram")
LIMIT = 3900

HELP = """Jarvis — команды:
Любой текст — новая задача (если ждёт ответ на уточнение — это ответ).
/new текст — точно новая задача
/answer T-0001 текст — ответ на уточнение
/status — состояние и бюджет
/tasks — последние задачи
/task T-0001 — ход работы по задаче
/stop — остановить всё; /resume — продолжить
/resume_models — снять паузу провайдера после пополнения
/fact ключ = значение — записать факт о себе (уровень 1)
/facts [поиск] — факты в памяти
/journal — прогнозы и Brier
/playbook — кандидаты в плейбук; /promote N, /reject N
/outcome T-0001 текст — что получилось по задаче на деле (Jarvis разберёт и запомнит уроки)
/cases — последние дела агента и их уроки"""


def split(text: str) -> list[str]:
    out = []
    while len(text) > LIMIT:
        cut = text.rfind("\n", 0, LIMIT)
        cut = cut if cut > 1000 else LIMIT
        out.append(text[:cut])
        text = text[cut:]
    out.append(text)
    return out


class TelegramBot:
    def __init__(self, token: str, owner_id: int, store: Store, *,
                 transport: httpx.AsyncBaseTransport | None = None, poll_timeout: int = 30):
        self.api = f"https://api.telegram.org/bot{token}"
        self.owner_id = owner_id
        self.store = store
        self.client = httpx.AsyncClient(transport=transport, timeout=poll_timeout + 15)
        self.poll_timeout = poll_timeout
        self.orch: Orchestrator | None = None
        self.memory: Memory | None = None
        self.journal: Journal | None = None
        self.treasury: Treasury | None = None
        self.guard: Guard | None = None
        self.gateway: ModelGateway | None = None

    # --- отправка ----------------------------------------------------------------
    async def notify(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        chunks = split(text)
        for i, chunk in enumerate(chunks):
            payload: dict = {"chat_id": self.owner_id, "text": chunk, "disable_web_page_preview": True}
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d}] for t, d in buttons]}
            try:
                await self.client.post(f"{self.api}/sendMessage", json=payload)
            except httpx.HTTPError as e:
                log.warning("sendMessage: %s", type(e).__name__)

    async def send_file(self, path: str, caption: str = "") -> None:
        try:
            with open(path, "rb") as f:
                await self.client.post(f"{self.api}/sendDocument", data={"chat_id": str(self.owner_id),
                                                                           "caption": caption[:1000]},
                                       files={"document": (path.rsplit("/", 1)[-1], f.read())})
        except (OSError, httpx.HTTPError) as e:
            log.warning("sendDocument: %s", type(e).__name__)

    # --- приём ---------------------------------------------------------------------
    async def poll_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except httpx.HTTPError as e:
                log.warning("getUpdates: %s", type(e).__name__)
                await asyncio.sleep(5)

    async def poll_once(self) -> int:
        offset = int(self.store.get("tg_offset", 0))
        resp = await self.client.get(f"{self.api}/getUpdates",
                                     params={"timeout": self.poll_timeout, "offset": offset})
        if resp.status_code != 200:  # 401 (токен), 409 (второй экземпляр бота) и т.п. — не молотим API
            log.warning("getUpdates: HTTP %s", resp.status_code)
            await asyncio.sleep(10)
            return 0
        updates = resp.json().get("result", [])
        for upd in updates:
            self.store.set("tg_offset", upd["update_id"] + 1)
            try:
                await self.handle(upd)
            except Exception as e:  # одна битая команда не должна ронять бота
                log.error("update failed: %s", type(e).__name__)
                await self.notify(f"Ошибка обработки: {type(e).__name__}")
        return len(updates)

    def _is_owner(self, user: dict | None) -> bool:
        return bool(user) and int(user.get("id", 0)) == int(self.owner_id)

    async def handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            if not self._is_owner(cq.get("from")):
                self.store.log_event("tg_stranger", {"from": (cq.get("from") or {}).get("id")})
                return
            await self.client.post(f"{self.api}/answerCallbackQuery", json={"callback_query_id": cq["id"]})
            data = cq.get("data", "")
            if data.startswith("ap:"):
                _, aid, choice = data.split(":", 2)
                await self.notify(await self.orch.decide(int(aid), choice))
            elif data.startswith("jr:"):
                _, jid, val = data.split(":", 2)
                self.journal.resolve(int(jid), val == "1")
                await self.notify("Записал исход в журнал решений.")
            return
        msg = upd.get("message") or {}
        if not self._is_owner(msg.get("from")):
            if msg:
                self.store.log_event("tg_stranger", {"from": (msg.get("from") or {}).get("id")})
            return
        text = (msg.get("text") or "").strip()
        if text:
            await self.on_text(text)

    async def on_text(self, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        arg = arg.strip()
        o = self.orch
        if cmd in ("/start", "/help"):
            await self.notify(HELP)
        elif cmd == "/stop":
            self.guard.stop()
            await self.notify("⏹ Всё остановлено. Служебные команды работают. /resume — продолжить.")
        elif cmd == "/resume":
            self.guard.resume()
            await self.notify("▶️ Продолжаю.")
        elif cmd == "/resume_models":
            self.gateway.resume_provider()
            await self.notify("Пауза провайдера снята.")
        elif cmd == "/status":
            await self.notify(self.status_text())
        elif cmd == "/tasks":
            rows = self.store.tasks(limit=12)
            await self.notify("\n".join(f"{r['id']} · {r['status']} · {r['cls'] or '?'} · {r['request'][:60]}"
                                        for r in rows) or "Задач пока нет.")
        elif cmd == "/task":
            await self.notify(self.trace_text(arg.upper()))
        elif cmd == "/new":
            await self._new(arg)
        elif cmd == "/answer":
            tid, _, ans = arg.partition(" ")
            ok = o.answer_clarification(tid.upper(), ans.strip())
            await self.notify("Принял ответ, продолжаю." if ok else "Эта задача не ждёт уточнения.")
        elif cmd == "/outcome":
            tid, _, txt = arg.partition(" ")
            if not txt.strip():
                await self.notify("Формат: /outcome T-0001 что получилось (цифры, что сработало, что нет)")
            else:
                await self.notify(await o.outcome(tid.upper(), txt.strip()))
        elif cmd == "/cases":
            rows = o.cases.recent(8)
            if not rows:
                await self.notify("Дел пока нет.")
            else:
                await self.notify("\n\n".join(
                    f"{c['task_id']} · {c['status']} · ${c['cost_usd'] or 0:.2f}\n{(c['title'] or '')[:120]}"
                    + "".join(f"\n  урок: {x['text'][:160]}" for x in c["lessons"][-3:]) for c in rows))
        elif cmd == "/fact":
            key, sep, value = arg.partition("=")
            if not sep or not key.strip():
                await self.notify("Формат: /fact ключ = значение")
                return
            res = self.memory.write_fact("owner", key.strip(), value.strip(), source="владелец в Telegram", tier=1)
            await self.notify({"created": "Записал.", "replaced": "Обновил (старое значение в истории).",
                               "same": "Уже так записано.", "conflict": "Конфликт — пришлю на решение."}[res])
        elif cmd == "/facts":
            rows = self.memory.facts(arg or None)
            await self.notify("\n".join(f"{r['namespace']}.{r['key']} = {r['value']} · ур.{r['tier']} · {r['source']}"
                                        for r in rows) or "Фактов нет.")
        elif cmd == "/journal":
            score, n = self.journal.brier()
            open_ = self.journal.entries(resolved=False)[:8]
            lines = [f"Brier: {score if score is not None else '—'} по {n} прогнозам"]
            lines += [f"#{e['id']} p={e['p']:.2f} до {e['resolve_by']}: {e['question'][:80]}" for e in open_]
            await self.notify("\n".join(lines))
        elif cmd == "/playbook":
            rows = self.memory.lessons("candidate")[:15]
            await self.notify("\n".join(f"#{r['id']} {r['text']}" for r in rows) or "Кандидатов нет.")
        elif cmd in ("/promote", "/reject") and arg.isdigit():
            status = "active" if cmd == "/promote" else "rejected"
            ok = self.memory.set_lesson_status(int(arg), status)
            await self.notify("Готово." if ok else "Не нашёл такой урок.")
        elif cmd.startswith("/"):
            await self.notify("Не знаю такой команды. /help")
        else:
            waiting = [t for t in self.store.tasks(statuses=["awaiting_clarification", "agent_waiting"])
                       if t["status"] == "awaiting_clarification" or '"type": "question"' in (t["result_json"] or "")]
            if len(waiting) == 1:
                o.answer_clarification(waiting[0]["id"], text)
                await self.notify(f"Принял как ответ к {waiting[0]['id']}. Новую задачу — через /new.")
            else:
                await self._new(text)

    async def _new(self, text: str) -> None:
        if not text:
            await self.notify("Пустая задача. Напиши, что нужно сделать.")
            return
        tid = self.orch.submit(text)
        stopped = " (сейчас стоп — начну после /resume)" if self.guard.stopped() else ""
        await self.notify(f"📥 {tid} принята{stopped}.")

    def status_text(self) -> str:
        t = self.treasury
        active = self.store.tasks(statuses=["new", "ready", "executing", "verifying", "rework"])
        waiting = self.store.tasks(statuses=["awaiting_owner", "awaiting_clarification"])
        score, n = self.journal.brier()
        return (f"{'⏹ СТОП' if self.guard.stopped() else '▶️ Работает'}"
                f"{' · провайдер на паузе' if self.gateway.is_paused() else ''}\n"
                f"Бюджет: сегодня ${t.spent_day():.2f} из ${t.cfg.day_usd:.0f}, "
                f"месяц ${t.spent_month():.2f} из ${t.cfg.month_usd:.0f}\n"
                f"В работе: {len(active)} · ждут тебя: {len(waiting)}\n"
                f"Brier: {score if score is not None else '—'} ({n})")

    def trace_text(self, tid: str) -> str:
        task = self.store.task(tid)
        if not task:
            return "Нет такой задачи."
        lines = [f"{tid} · {task['status']} · класс {task['cls'] or '?'} · круг {task['round']}", task["request"][:200]]
        for s in self.store.steps(tid)[-25:]:
            cost = f" ${s['cost_usd']:.3f}" if s["cost_usd"] else ""
            lines.append(f"· {s['kind']} [{s['actor']}{' ' + s['model'] if s['model'] else ''}]{cost}: {s['detail'][:140]}")
        return "\n".join(lines)
