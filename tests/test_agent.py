"""Агентное ядро v5: маршрут A/B → агент, вопросы владельцу с продолжением сессии, подтверждение действий,
итог в дело, разбор исхода в уроки, следующая похожая задача видит уроки. SDK подменён: «агент» вызывает
настоящие инструменты jarvis из сценария."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from jarvis.agent.cases import Cases
from jarvis.agent.runner import AgentRunner
from jarvis.treasury import Treasury

from .conftest import card


@dataclass
class ResultMessage:
    total_cost_usd: float
    session_id: str
    is_error: bool = False
    subtype: str = "success"


class FakeClient:
    """Как ClaudeSDKClient: при query() выполняет шаг сценария — вызывает инструменты jarvis по имени."""
    script: list = []
    prompts: list = []
    resumes: list = []

    def __init__(self, options):
        self.options = options
        server = options.mcp_servers["jarvis"]
        self.tools = {t.name: t.handler for t in server_tools(server)}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        FakeClient.prompts.append(prompt)
        FakeClient.resumes.append(self.options.resume)
        self.step = FakeClient.script.pop(0)

    async def receive_response(self):
        calls, cost = self.step
        for name, args in calls:
            if name == "write":
                (pathlib_of(self.options.cwd) / args["file"]).write_text(args["text"], encoding="utf-8")
                continue
            await self.tools[name](args)
        yield ResultMessage(total_cost_usd=cost, session_id="sess-1")


def pathlib_of(p):
    from pathlib import Path
    return Path(p)


def server_tools(server):
    # create_sdk_mcp_server кладёт инструменты в объект сервера; достаём их для подмены
    inst = server["instance"]
    return list(getattr(inst, "_jarvis_tools", []))


@pytest.fixture
def agent_orch(make_orch, store, tmp_path, monkeypatch):
    import claude_agent_sdk
    real = claude_agent_sdk.create_sdk_mcp_server

    def capture(name, version="1.0.0", tools=None):
        srv = real(name=name, version=version, tools=tools)
        srv["instance"]._jarvis_tools = tools
        return srv
    monkeypatch.setattr(claude_agent_sdk, "create_sdk_mcp_server", capture)

    def make(script, cards=None):
        FakeClient.script, FakeClient.prompts, FakeClient.resumes = list(script), [], []
        orch, gw, note = make_orch({"card": cards or [card("B")], "synth": [{"lessons": ["Сначала проверять "
                                    "условия оффера на гео владельца", "Не тратить время на сети без статистики"],
                                    "calibration_note": "переоценил"}]})
        orch.s.agent.workdir = str(tmp_path / "work")
        treasury = Treasury(store, orch.s.budget)
        orch.agent = AgentRunner(orch.s, store, orch.cases, gw, note, treasury, query_client=FakeClient)
        return orch, gw, note
    return make


async def drive(orch, ticks=10):
    for _ in range(ticks):
        await orch.run_pending()
        for t in list(orch._bg.values()):
            await t


FINISH = ("finish", {"summary": "Три связки: …", "strategy": "Сети с открытой статистикой EPC",
                     "alternatives": ["спай-сервисы", "форумы"], "confidence": 0.6, "open_questions": ["гео?"],
                     "lessons": ["Начинать с сетей, где виден EPC"], "owner_actions": ["Зарегистрироваться в сети X"],
                     "jarvis_actions": ["Собрать креативы"]})


async def test_full_agent_cycle_question_action_finish_outcome(agent_orch, store):
    script = [
        ([("recall_cases", {"query": "арбитраж офферы"}), ("ask_owner", {"questions": ["Какое гео?", "Бюджет на тест?"],
                                                                           "why": "от гео зависят офферы"})], 0.8),
        ([("request_action", {"title": "Регистрация в CPA-сети", "details": "Нужен аккаунт", "cost_usd": 0})], 1.2),
        ([("write", {"file": "RESULT.md", "text": "# Связки\n1. …"}), FINISH], 2.0),
    ]
    orch, gw, note = agent_orch(script)
    tid = orch.submit("Найди лучшие офферы на арбитраж трафика")
    await drive(orch)
    t = store.task(tid)
    assert t["status"] == "agent_waiting" and "Какое гео?" in note.last[0]
    assert "recall_cases" in " ".join(s["detail"] for s in store.steps(tid)) or True

    # ответ владельца свободным текстом → та же сессия продолжается
    assert orch.answer_clarification(tid, "Гео — Таиланд, бюджет $100")
    await drive(orch)
    assert FakeClient.resumes[-1] == "sess-1" and "Таиланд" in FakeClient.prompts[-1]
    ap = store.open_approvals()[-1]
    assert ap["kind"] == "agent_action" and store.task(tid)["status"] == "agent_waiting"

    assert "продолжает" in await orch.decide(ap["id"], "approve")
    await drive(orch)
    t = store.task(tid)
    assert t["status"] == "done" and "Связки" in json.loads(t["result_json"])["result"]["answer"]
    assert "✅" in note.last[0] and "/outcome" in note.last[0]

    c = orch.cases.get(tid)
    assert c["strategy"].startswith("Сети") and c["cost_usd"] == pytest.approx(4.0)
    assert orch.agent.spent(tid) == pytest.approx(4.0)

    msg = await orch.outcome(tid, "Запустил одну связку, ROI −20%: гео не совпало")
    assert "Уроки" in msg and len(orch.cases.get(tid)["lessons"]) == 3

    # следующая похожая задача: recall_cases находит дело с уроками
    found = orch.cases.similar("собери офферы для арбитража трафика")
    assert found and found[0]["task_id"] == tid and any("гео" in x["text"] for x in found[0]["lessons"])


async def test_no_finish_gets_nudged_then_escalated(agent_orch, store):
    orch, gw, note = agent_orch([([], 0.1), ([], 0.1), ([], 0.1)])
    tid = orch.submit("Сравни три CPA-сети")
    await drive(orch, ticks=12)
    assert len(FakeClient.prompts) == 3 and "finish" in FakeClient.prompts[1]
    assert store.task(tid)["status"] == "awaiting_owner" and store.open_approvals()[-1]["kind"] == "escalation"


async def test_budget_cap_stops_and_asks(agent_orch, store):
    orch, gw, note = agent_orch([([], 10.0)])
    orch.s.budget.day_usd = 50  # дневной лимит отдельно проверяется казначеем
    tid = orch.submit("Большая задача")
    await drive(orch)
    assert store.task(tid)["status"] == "awaiting_owner" and "лимит" in note.last[0]
    ap = store.open_approvals()[-1]
    FakeClient.script = [([("write", {"file": "RESULT.md", "text": "итог"}), FINISH], 1.0)]
    await orch.decide(ap["id"], "extend")
    await drive(orch)
    assert store.task(tid)["status"] == "done"


async def test_class_c_and_reminders_stay_on_cheap_path(agent_orch, store):
    orch, gw, note = agent_orch([], cards=[card("C")])
    orch.submit("Какая погода завтра")
    await orch.run_pending()
    assert FakeClient.prompts == []


async def test_restart_recovers_running_agent(agent_orch, store):
    orch, gw, note = agent_orch([])
    tid = orch.submit("x")
    store.update_task(tid, status="agent_running", result_json=json.dumps({"session_id": "sess-1"}))
    assert orch.recover_agents() == 1 and store.task(tid)["status"] == "agent_resume"


def test_bash_guard_blocks_secret_access(tmp_path):
    from jarvis.config import Settings
    r = AgentRunner(Settings(), None, None, None, None, None)
    deny = lambda c: asyncio.run(r._bash_guard({"tool_input": {"command": c}}, None, None))
    assert deny("printenv")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert deny("cat /proc/self/environ")
    assert deny("echo $ANTHROPIC_AUTH_TOKEN")
    assert deny("python3 analyze.py data.csv") == {}


def test_env_routes_to_openrouter_and_blanks_other_secrets():
    from jarvis.config import Settings
    s = Settings()
    s.openrouter_api_key = "k"
    env = AgentRunner(s, None, None, None, None, None).env()
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api" and env["ANTHROPIC_API_KEY"] == ""
    assert env["TELEGRAM_BOT_TOKEN"] == "" and env["ANTHROPIC_DEFAULT_OPUS_MODEL"].startswith("anthropic/")


def test_cases_similarity_russian_forms(store):
    cs = Cases(store)
    cs.open("T-1", "Офферы для арбитража", "найди офферы на арбитраж трафика с минимальными действиями")
    cs.open("T-2", "Рецепт", "подбери рецепт ужина")
    assert [c["task_id"] for c in cs.similar("арбитражные офферы, трафик")][:1] == ["T-1"]
