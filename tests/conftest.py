from __future__ import annotations

import json
from typing import Any, Callable

import pytest

from jarvis.config import Settings
from jarvis.gateway import AllCandidatesFailed, Reply, vendor_of
from jarvis.guard import Guard
from jarvis.journal import Journal
from jarvis.memory import Memory
from jarvis.orchestrator import Orchestrator
from jarvis.store import Store


class ScriptedGateway:
    """Подставной шлюз: роль → очередь ответов (dict | Exception | callable(messages) -> dict).

    Последний ответ в очереди «залипает» и повторяется. Учитывает exclude_vendors,
    как настоящий шлюз: если модель роли исключена, берёт запасную другого вендора.
    """

    MODELS = {"critic": ["openai/gpt-6-luna", "google/gemini-3.8-flash"],
              "worker": ["deepseek/deepseek-v4.1-flash"]}
    DEFAULT = ["anthropic/claude-sonnet-5.5", "openai/gpt-6-luna"]

    def __init__(self, script: dict[str, list[Any]], cost: float = 0.01):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict] = []
        self.cost = cost

    async def chat(self, role, messages, *, task_id=None, max_tokens=1500, schema=None,
                   exclude_vendors=(), task_cap=None, temperature=0.2) -> Reply:
        models = [m for m in self.MODELS.get(role, self.DEFAULT) if vendor_of(m) not in exclude_vendors]
        self.calls.append({"role": role, "exclude": tuple(exclude_vendors), "messages": messages,
                           "task_id": task_id})
        if not models:
            raise AllCandidatesFailed(f"нет моделей для {role}")
        queue = self.script.get(role)
        if not queue:
            raise AssertionError(f"нет заготовленного ответа для роли {role}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(messages)
        data = schema.model_validate(item) if schema else None
        return Reply(json.dumps(item, ensure_ascii=False), data, models[0], vendor_of(models[0]), self.cost)

    def roles(self) -> list[str]:
        return [c["role"] for c in self.calls]


class CollectNotifier:
    def __init__(self):
        self.messages: list[tuple[str, list | None]] = []

    async def notify(self, text: str, buttons=None) -> None:
        self.messages.append((text, buttons))

    @property
    def last(self) -> tuple[str, list | None]:
        return self.messages[-1]


@pytest.fixture
def settings() -> Settings:
    s = Settings()
    s.owner.rules = ["Деньги — только с согласия владельца"]
    return s


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


@pytest.fixture
def make_orch(settings, store) -> Callable[..., tuple[Orchestrator, ScriptedGateway, CollectNotifier]]:
    def make(script: dict, **kw) -> tuple[Orchestrator, ScriptedGateway, CollectNotifier]:
        gw = ScriptedGateway(script)
        notifier = CollectNotifier()
        orch = Orchestrator(settings, store, gw, Memory(store), Journal(store), Guard(store), notifier, **kw)
        return orch, gw, notifier
    return make


# --- типовые ответы моделей ---------------------------------------------------------------

def card(cls="B", questions=None, privacy="redacted_cloud", forecast=None, evidence=True):
    return {"goal": "Цель задачи", "result_image": "Короткий ответ с вариантами",
            "acceptance": ["Есть рекомендация"], "questions": questions or [], "cls": cls,
            "privacy": privacy, "domain": "general", "needs_evidence": evidence,
            "forecast_question": forecast}


def result(answer="Подробный ответ на задачу владельца с рекомендацией.", actions=None, p=None,
           url="https://www.immigration.go.th/rules"):
    claims = [{"text": "Факт из источника", "source_url": url}] if url else []
    return {"answer": answer, "claims": claims, "owner_actions": actions or [], "forecast_p": p,
            "open_questions": []}


def verdict(passed=True, severity="none", issues=None):
    return {"passed": passed, "severity": severity, "issues": issues or []}


PLAN = {"subtasks": [{"title": "Правила", "question": "Какие правила?", "query": "правила"},
                     {"title": "Сроки", "question": "Какие сроки?", "query": "сроки"}]}
FINDING = {"finding": "Нашёл ответ", "claims": [{"text": "Факт", "source_url": "https://www.immigration.go.th/x"}],
           "confidence": 0.8}
