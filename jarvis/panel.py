"""Мост к панели через Notion (как у MMachine): портов наружу нет, доступ — только через аккаунт владельца.

Раз в минуту Jarvis пишет снимок состояния (gzip+base64 JSON) в код-блок страницы «Jarvis — состояние».
Панель (артефакт claude.ai) читает его через Notion и пишет команды строками в базу «Jarvis — команды»;
Jarvis забирает строки со статусом «новая», выполняет их как команды из Telegram и пишет ответ.
Секреты в снимок не попадают; тексты задач — да (это данные владельца в его же Notion).
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
from datetime import datetime, timezone

import httpx

from .guard import scrub_secrets
from .store import Store

log = logging.getLogger("jarvis.panel")
API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
CHUNK = 1900


def snapshot(app) -> dict:
    """Всё, что показывает панель: главная, задачи, память, система."""
    s: Store = app.store
    tasks = []
    for t in s.tasks(limit=40):
        card = Store.loads(t["card_json"], {}) or {}
        res = Store.loads(t["result_json"], {}) or {}
        steps = s.steps(t["id"])
        tasks.append({
            "id": t["id"], "status": t["status"], "cls": t["cls"], "round": t["round"],
            "request": (t["request"] or "")[:300], "goal": (card.get("goal") or "")[:300],
            "note": (t["note"] or "")[:300], "created": t["created_at"], "updated": t["updated_at"],
            "usd": round(sum(x["cost_usd"] or 0 for x in steps), 4),
            "answer": (res.get("answer") or "")[:4000],
            "owner_actions": res.get("owner_actions", [])[:10],
            "issues": (Store.loads(t["issues_json"], []) or [])[:6],
            "questions": (Store.loads(t["questions_json"], []) or [])[:6],
            "steps": [{"t": x["created_at"][11:16], "kind": x["kind"], "actor": x["actor"],
                       "model": x["model"], "detail": (x["detail"] or "")[:200], "usd": x["cost_usd"]}
                      for x in steps[-25:]],
        })
    approvals = [{"id": a["id"], "task_id": a["task_id"], "kind": a["kind"],
                  "payload": Store.loads(a["payload_json"], {}), "created": a["created_at"]}
                 for a in s.open_approvals()]
    score, n = app.journal.brier()
    by_model = s.all("SELECT model, COUNT(*) n, ROUND(SUM(usd),4) usd FROM spend WHERE month=? GROUP BY model "
                     "ORDER BY usd DESC", (datetime.now(timezone.utc).strftime("%Y-%m"),))
    rework = app.orch.rework.stats(7)
    data = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "owner": app.settings.owner.name,
        "stopped": app.guard.stopped(),
        "budget": {"day": round(app.treasury.spent_day(), 4), "day_cap": app.settings.budget.day_usd,
                   "month": round(app.treasury.spent_month(), 4), "month_cap": app.settings.budget.month_usd,
                   "task_caps": app.settings.budget.task_usd, "by_model": by_model},
        "tasks": tasks,
        "approvals": approvals,
        "reminders": [{"id": r["id"], "text": r["text"], "due_utc": r["due_utc"]}
                      for r in app.orch.reminders.pending()[:20]],
        "memory": {
            "facts": [{"id": f["id"], "ns": f["namespace"], "key": f["key"], "value": f["value"],
                       "source": f["source"], "tier": f["tier"], "valid_until": f["valid_until"]}
                      for f in app.memory.facts(limit=60)],
            "lessons": [{"id": x["id"], "text": x["text"]} for x in app.memory.lessons("active")],
            "candidates": [{"id": x["id"], "text": x["text"], "source": x["source_task"]}
                           for x in app.memory.lessons("candidate")[:20]],
        },
        "journal": {"brier": score, "n": n, "calibration": app.journal.calibration(),
                    "open": [{"id": e["id"], "q": e["question"], "p": e["p"], "by": e["resolve_by"]}
                             for e in app.journal.entries(resolved=False)[:15]]},
        "rework": rework,
        "models": app.settings.models,
        "health": {"deployed": _read("/var/lib/jarvis/.deployed-commit"),
                   "backup": _read("/var/lib/jarvis/.last-backup"),
                   "searxng": bool(app.settings.searxng_url), "local_llm": bool(app.settings.local_llm_url)},
    }
    return json.loads(scrub_secrets(json.dumps(data, ensure_ascii=False, default=str)))


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def encode(data: dict) -> list[str]:
    raw = base64.b64encode(gzip.compress(json.dumps(data, ensure_ascii=False).encode())).decode()
    return [raw[i:i + CHUNK] for i in range(0, len(raw), CHUNK)] or [""]


def decode(chunks: list[str]) -> dict:
    return json.loads(gzip.decompress(base64.b64decode("".join(chunks))))


class NotionPanel:
    def __init__(self, token: str, state_page: str, commands_db: str, client: httpx.AsyncClient | None = None):
        self.state_page, self.commands_db = state_page, commands_db
        self.client = client or httpx.AsyncClient(timeout=30, headers={
            "Authorization": f"Bearer {token}", "Notion-Version": VERSION, "Content-Type": "application/json"})
        self._code_block: str | None = None

    async def _find_code_block(self) -> str:
        r = await self.client.get(f"{API}/blocks/{self.state_page}/children", params={"page_size": 100})
        r.raise_for_status()
        for b in r.json().get("results", []):
            if b.get("type") == "code":
                return b["id"]
        r = await self.client.patch(f"{API}/blocks/{self.state_page}/children", json={"children": [
            {"object": "block", "type": "code", "code": {"language": "plain text", "rich_text": []}}]})
        r.raise_for_status()
        return r.json()["results"][0]["id"]

    async def publish(self, data: dict) -> None:
        if not self._code_block:
            self._code_block = await self._find_code_block()
        rich = [{"type": "text", "text": {"content": c}} for c in encode(data)][:100]
        r = await self.client.patch(f"{API}/blocks/{self._code_block}",
                                    json={"code": {"language": "plain text", "rich_text": rich}})
        if r.status_code == 404:  # блок удалили руками — найдём или создадим заново
            self._code_block = None
        r.raise_for_status()

    async def pending_commands(self) -> list[dict]:
        r = await self.client.post(f"{API}/databases/{self.commands_db}/query", json={
            "filter": {"property": "Статус", "select": {"equals": "новая"}},
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}], "page_size": 20})
        r.raise_for_status()
        out = []
        for p in r.json().get("results", []):
            pr = p["properties"]
            out.append({"page": p["id"],
                        "type": ((pr.get("Тип") or {}).get("select") or {}).get("name", ""),
                        "title": "".join(x["plain_text"] for x in (pr.get("Команда") or {}).get("title", [])),
                        "arg": "".join(x["plain_text"] for x in (pr.get("Аргумент") or {}).get("rich_text", []))})
        return out

    async def answer(self, page: str, ok: bool, text: str) -> None:
        r = await self.client.patch(f"{API}/pages/{page}", json={"properties": {
            "Статус": {"select": {"name": "выполнена" if ok else "ошибка"}},
            "Ответ": {"rich_text": [{"type": "text", "text": {"content": scrub_secrets(text)[:1900]}}]}}})
        r.raise_for_status()


async def run_command(app, cmd: dict) -> tuple[bool, str]:
    """Команды панели = команды Telegram. Аргументы — как в Telegram."""
    kind, arg = cmd["type"], (cmd["arg"] or cmd["title"]).strip()
    if kind == "task":
        if not arg:
            return False, "пустой запрос"
        tid = app.orch.submit(arg)
        return True, f"принято: {tid}"
    if kind == "answer":  # ответы на уточняющие вопросы: «T-0001 | ответ»
        tid, _, text = arg.partition("|")
        app.orch.answer_clarification(tid.strip(), text.strip())
        return True, "ответ передан"
    if kind == "decide":  # «12 accept»
        aid, _, choice = arg.partition(" ")
        return True, await app.orch.decide(int(aid), choice.strip())
    if kind == "stop":
        app.guard.stop()
        return True, "⏹ остановлено"
    if kind == "resume":
        app.guard.resume()
        return True, "▶️ продолжаю"
    if kind == "fact":  # «ключ = значение»
        key, _, value = arg.partition("=")
        if not value.strip():
            return False, "формат: ключ = значение"
        res = app.memory.write_fact("owner", key.strip(), value.strip(), source="владелец в панели", tier=1)
        return True, f"факт: {res}"
    if kind in ("promote", "reject"):
        ok = app.memory.set_lesson_status(int(arg), "active" if kind == "promote" else "rejected")
        return ok, "готово" if ok else "нет такого кандидата"
    if kind == "journal":  # «7 1» — прогноз 7 сбылся (1) / нет (0)
        jid, _, val = arg.partition(" ")
        app.journal.resolve(int(jid), val.strip() == "1")
        return True, "исход записан"
    return False, f"неизвестная команда: {kind}"


async def panel_loop(app, panel: NotionPanel, period: float = 60.0, poll: float = 15.0) -> None:
    import asyncio
    last_pub = 0.0
    while True:
        changed = False
        try:
            for cmd in await panel.pending_commands():
                try:
                    ok, text = await run_command(app, cmd)
                except Exception as e:  # noqa: BLE001 — ошибка одной команды не роняет мост
                    ok, text = False, f"{type(e).__name__}: {e}"
                await panel.answer(cmd["page"], ok, text)
                changed = True
            now = asyncio.get_running_loop().time()
            if changed or now - last_pub >= period:
                await panel.publish(snapshot(app))
                last_pub = now
        except Exception as e:  # noqa: BLE001 — Notion недоступен: панель подождёт, Jarvis работает
            log.warning("панель: %s", type(e).__name__)
        await asyncio.sleep(poll)
