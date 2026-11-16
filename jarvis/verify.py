"""Проверка результата: чек-лист в коде + критик другого вендора с чистым контекстом.

Критик видит только карточку, результат и правила владельца — не ход работы и не
рассуждения автора. Решение «принято/нет» принимает код, а не ещё одна LLM-арбитр.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .card import Result, TaskCard, TaskClass
from .config import Settings
from .gateway import Gateway, Reply, as_messages, vendor_of
from .guard import leaks_secret, redact
from .prompts import CRITIC_SYSTEM, owner_block


class CriticVerdict(BaseModel):
    passed: bool
    severity: Literal["none", "minor", "major", "critical"] = "none"
    issues: list[str] = Field(default_factory=list)


def checklist(card: TaskCard, result: Result) -> list[str]:
    """Проверяемые кодом условия приёмки. Возвращает список нарушений."""
    problems: list[str] = []
    if len(result.answer.strip()) < 20:
        problems.append("Ответ пустой или слишком короткий")
    if leaks_secret(result.answer) or any(leaks_secret(a) for a in result.owner_actions):
        problems.append("В ответе похоже на секрет (ключ/токен) — выводить нельзя")
    if card.evidence.required:
        sourced = [c for c in result.claims if c.source_url]
        if not sourced:
            problems.append("Нет ни одного утверждения с источником, а карточка требует доказательств")
        elif card.cls == TaskClass.A:
            unsourced = [c for c in result.claims if not c.source_url]
            if unsourced:
                problems.append(f"Класс A: {len(unsourced)} утверждений без источника")
            if not any((c.tier or 3) <= card.evidence.min_tier for c in sourced):
                problems.append(f"Класс A: нет ни одного источника уровня {card.evidence.min_tier}")
    if card.cls == TaskClass.A and not result.owner_actions:
        problems.append("Класс A: не указано, что владелец делает сам")
    if card.forecast_question and result.forecast_p is None:
        problems.append("В карточке есть прогноз, а вероятность не дана")
    if result.forecast_p is not None and not 0 <= result.forecast_p <= 1:
        problems.append("Вероятность прогноза вне диапазона 0..1")
    return problems


async def critic_review(card: TaskCard, result: Result, author_model: str, *, gw: Gateway,
                        settings: Settings) -> tuple[CriticVerdict, Reply]:
    system = CRITIC_SYSTEM.format(owner=owner_block(settings.owner.rules))
    user = {"карточка": {"цель": card.goal, "образ_результата": card.result_image,
                         "критерии": card.acceptance, "класс": card.cls.value},
            "результат": redact(result.model_dump_json())}
    reply = await gw.chat("critic", as_messages(system, user), task_id=card.id, schema=CriticVerdict,
                          exclude_vendors=(vendor_of(author_model),), task_cap=card.budget.usd_cap,
                          max_tokens=900, temperature=0.0)
    return reply.data, reply


def accepted(problems: list[str], verdict: CriticVerdict) -> bool:
    return not problems and verdict.passed and verdict.severity in ("none", "minor")
