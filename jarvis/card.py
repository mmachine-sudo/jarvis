"""Карточка задачи — единый контракт между Jarvis, ядром и исполнителями."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TaskClass(str, Enum):
    A = "A"  # деньги, право, необратимое — решает владелец
    B = "B"  # важное, обратимое
    C = "C"  # быт

    @property
    def rank(self) -> int:
        return {"C": 0, "B": 1, "A": 2}[self.value]


class Mode(str, Enum):
    SOLO = "solo"
    TEAM = "team"


class Privacy(str, Enum):
    LOCAL_ONLY = "local_only"
    REDACTED_CLOUD = "redacted_cloud"
    CLOUD_OK = "cloud_ok"


class Autonomy(str, Enum):
    L0 = "L0"  # только предлагает
    L1 = "L1"  # обратимые действия
    L2 = "L2"  # траты в пределах лимита
    L3 = "L3"  # только по кнопке владельца


class EvidencePolicy(BaseModel):
    required: bool = True
    min_tier: int = 2          # 1 — лучший (официальный источник)
    max_age_days: int = 90


class TaskBudget(BaseModel):
    usd_cap: float
    max_rounds: int = 2
    depth_cap: int = 3
    fanout_cap: int = 4


class TaskCard(BaseModel):
    id: str
    goal: str
    result_image: str
    acceptance: list[str] = Field(default_factory=list)
    cls: TaskClass = TaskClass.B
    mode: Mode = Mode.TEAM
    privacy: Privacy = Privacy.REDACTED_CLOUD
    autonomy: Autonomy = Autonomy.L1
    evidence: EvidencePolicy = Field(default_factory=EvidencePolicy)
    budget: TaskBudget
    domain: str = "general"
    forecast_question: str | None = None
    parent_id: str | None = None
    depth: int = 0


# --- Результаты исполнителей --------------------------------------------------

class Claim(BaseModel):
    text: str
    source_url: str | None = None
    tier: int | None = None  # заполняет код по домену, не модель


class Result(BaseModel):
    answer: str
    claims: list[Claim] = Field(default_factory=list)
    owner_actions: list[str] = Field(default_factory=list)
    forecast_p: float | None = None
    open_questions: list[str] = Field(default_factory=list)


MODE_BY_CLASS = {TaskClass.A: Mode.TEAM, TaskClass.B: Mode.TEAM, TaskClass.C: Mode.SOLO}
AUTONOMY_BY_CLASS = {TaskClass.A: Autonomy.L0, TaskClass.B: Autonomy.L1, TaskClass.C: Autonomy.L2}
