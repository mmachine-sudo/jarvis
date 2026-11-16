"""Режимы исполнения: Solo (одна модель) и Team (оркестратор + субагенты, только чтение).

Протокол точного решения (ПТР) — фаза 2; в фазе 1 задачи класса A идут через Team,
проверку критиком другого вендора и обязательное решение владельца.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel, Field

from .card import Claim, Result, TaskCard
from .config import Settings
from .gateway import Gateway, ModelError, Reply, as_messages
from .memory import tier_of
from .prompts import PLANNER_SYSTEM, SOLO_SYSTEM, SYNTH_SYSTEM, WORKER_SYSTEM, owner_block
from .reader import Evidence, QuarantineReader, Searcher
from .treasury import BudgetExceeded

StepLog = Callable[[str, str, str, Reply | None], None]


class SubTask(BaseModel):
    title: str
    question: str
    query: str = ""


class Plan(BaseModel):
    subtasks: list[SubTask] = Field(default_factory=list)


class Finding(BaseModel):
    finding: str
    claims: list[Claim] = Field(default_factory=list)
    confidence: float = 0.5


@dataclass
class ExecContext:
    card: TaskCard
    briefing: list[dict]
    rework_notes: list[str]
    lessons: list[str]
    settings: Settings
    gw: Gateway
    log: StepLog
    reader: QuarantineReader | None = None
    searcher: Searcher | None = None
    security_flags: list[str] = field(default_factory=list)
    author: str = ""  # модель, написавшая итоговый результат (критик будет другого вендора)

    @property
    def cap(self) -> float:
        return self.card.budget.usd_cap

    def task_payload(self) -> dict:
        c = self.card
        payload = {
            "карточка": {"цель": c.goal, "образ_результата": c.result_image, "критерии": c.acceptance,
                         "класс": c.cls.value, "прогноз": c.forecast_question},
            "брифинг": self.briefing,
        }
        if self.rework_notes:
            payload["замечания_к_прошлой_версии"] = self.rework_notes
        return payload

    def owner(self) -> str:
        return owner_block(self.settings.owner.rules, self.lessons)


def stamp_tiers(result: Result, settings: Settings) -> Result:
    """Уровень источника ставит код по домену — модели в этом не доверяем."""
    for c in result.claims:
        c.tier = tier_of(c.source_url, settings.sources) if c.source_url else None
    return result


async def run_solo(ctx: ExecContext) -> Result:
    system = SOLO_SYSTEM.replace("{owner}", ctx.owner())
    reply = await ctx.gw.chat("solo", as_messages(system, ctx.task_payload()), task_id=ctx.card.id,
                              schema=Result, task_cap=ctx.cap, max_tokens=2500)
    ctx.log("execute", "solo", f"ответ получен ({len(reply.data.answer)} симв.)", reply)
    ctx.author = reply.model
    return stamp_tiers(reply.data, ctx.settings)


async def _research(ctx: ExecContext, sub: SubTask) -> list[Evidence]:
    if not (ctx.searcher and ctx.reader and sub.query):
        return []
    try:
        urls = await ctx.searcher.search(sub.query, n=3)
    except Exception as e:  # поиск недоступен — работаем без веба
        ctx.log("search", "searxng", f"поиск недоступен: {type(e).__name__}", None)
        return []
    evidence: list[Evidence] = []
    for url in urls:
        try:
            ev = await ctx.reader.read(url, sub.question, task_id=ctx.card.id, task_cap=ctx.cap)
        except (BudgetExceeded, ModelError):
            raise
        except Exception as e:
            ctx.log("read", "reader", f"{url}: не прочитано ({type(e).__name__})", None)
            continue
        if ev.instructions_detected:
            ctx.security_flags.append(f"На странице {url} найдены инструкции для агента — проигнорированы")
            ctx.log("security", "reader", f"инструкции на странице проигнорированы: {url}", None)
        evidence.append(ev)
    return evidence


async def run_team(ctx: ExecContext) -> Result:
    card = ctx.card
    planner = PLANNER_SYSTEM.format(fanout=card.budget.fanout_cap, owner=ctx.owner())
    plan_reply = await ctx.gw.chat("planner", as_messages(planner, ctx.task_payload()), task_id=card.id,
                                   schema=Plan, task_cap=ctx.cap, max_tokens=900)
    subs = plan_reply.data.subtasks[: card.budget.fanout_cap]
    ctx.log("plan", "planner", "; ".join(s.title for s in subs) or "без подзадач", plan_reply)

    async def one(sub: SubTask) -> tuple[SubTask, Finding, list[Evidence]]:
        evidence = await _research(ctx, sub)
        user = {"подвопрос": sub.question, "контекст_задачи": card.goal,
                "EVIDENCE": [e.model_dump() for e in evidence]}
        r = await ctx.gw.chat("worker", as_messages(WORKER_SYSTEM, user), task_id=card.id,
                              schema=Finding, task_cap=ctx.cap, max_tokens=1200)
        ctx.log("subtask", "worker", f"{sub.title}: готово", r)
        return sub, r.data, evidence

    results = await asyncio.gather(*(one(s) for s in subs), return_exceptions=True)
    findings = []
    for res in results:
        if isinstance(res, (BudgetExceeded, ModelError)):
            raise res
        if isinstance(res, BaseException):
            ctx.log("subtask", "worker", f"подзадача упала: {type(res).__name__}", None)
            continue
        sub, f, evidence = res
        findings.append({"подвопрос": sub.title, "находка": f.finding,
                         "утверждения": [c.model_dump() for c in f.claims], "уверенность": f.confidence,
                         "источники": [{"url": e.url, "уровень": e.tier} for e in evidence]})

    synth_user = ctx.task_payload() | {"находки_субагентов": findings}
    if ctx.security_flags:
        synth_user["предупреждения_безопасности"] = ctx.security_flags
    system = SYNTH_SYSTEM.replace("{owner}", ctx.owner())
    reply = await ctx.gw.chat("synth", as_messages(system, synth_user), task_id=card.id,
                              schema=Result, task_cap=ctx.cap, max_tokens=2500)
    ctx.log("synth", "synth", f"сведено {len(findings)} находок", reply)
    ctx.author = reply.model
    return stamp_tiers(reply.data, ctx.settings)

