"""Приём задачи: запрос владельца → карточка (или уточняющие вопросы)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .card import (AUTONOMY_BY_CLASS, MODE_BY_CLASS, EvidencePolicy, Privacy, TaskBudget,
                   TaskCard, TaskClass)
from .config import Settings
from .gateway import Gateway, as_messages
from .guard import redact
from .memory import Memory
from .prompts import CARD_SYSTEM, owner_block
from .router import classify, merge_class


class CardDraft(BaseModel):
    goal: str
    result_image: str
    acceptance: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    cls: Literal["A", "B", "C"] = "B"
    privacy: Literal["local_only", "redacted_cloud", "cloud_ok"] = "redacted_cloud"
    domain: str = "general"
    needs_evidence: bool = True
    forecast_question: str | None = None


async def draft_card(task_id: str, request: str, answers: list[str], *, gw: Gateway, memory: Memory,
                     settings: Settings, allow_questions: bool) -> tuple[TaskCard | None, list[str], float, str]:
    """Возвращает (карточка | None, вопросы, стоимость, модель)."""
    briefing = memory.briefing(request + " " + " ".join(answers))
    system = CARD_SYSTEM.format(max_q=settings.limits.clarify_max_questions,
                                owner=owner_block(settings.owner.rules))
    user = {"запрос": redact(request), "ответы_владельца": [redact(a) for a in answers],
            "брифинг": briefing}
    # разбор запроса — накладной расход до того, как известен класс: в лимит задачи не входит
    # (эталоны 16.11: карточка съедала половину лимита бытовой задачи $0.02)
    reply = await gw.chat("card", as_messages(system, user), task_id=f"{task_id}:intake", schema=CardDraft,
                          task_cap=settings.task_cap("A"), max_tokens=1200)
    d: CardDraft = reply.data
    questions = d.questions[: settings.limits.clarify_max_questions] if allow_questions else []
    if questions:
        return None, questions, reply.usd, reply.model

    cls = merge_class(classify(request, settings.router), TaskClass(d.cls))
    acceptance = d.acceptance or ["Ответ отвечает на запрос владельца"]
    card = TaskCard(
        id=task_id, goal=d.goal, result_image=d.result_image, acceptance=acceptance, cls=cls,
        mode=MODE_BY_CLASS[cls], privacy=Privacy(d.privacy), autonomy=AUTONOMY_BY_CLASS[cls],
        evidence=EvidencePolicy(required=d.needs_evidence and cls != TaskClass.C,
                                min_tier=1 if cls == TaskClass.A else 2),
        budget=TaskBudget(usd_cap=settings.task_cap(cls.value), max_rounds=1 if cls.value == "A" else settings.limits.max_rounds,
                          depth_cap=settings.limits.depth_cap, fanout_cap=settings.limits.fanout_cap),
        domain=d.domain or "general", forecast_question=d.forecast_question,
    )
    return card, [], reply.usd, reply.model
