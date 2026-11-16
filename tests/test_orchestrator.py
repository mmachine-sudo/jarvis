from __future__ import annotations

from jarvis.card import TaskClass
from jarvis.gateway import ProviderPaused
from jarvis.orchestrator import Orchestrator
from jarvis.reader import Evidence, ExtractedFact
from jarvis.treasury import BudgetExceeded

from .conftest import FINDING, PLAN, card, result, verdict


async def drive(orch: Orchestrator, ticks: int = 10) -> None:
    for _ in range(ticks):
        if not await orch.run_pending():
            break


async def test_class_c_solo_done(make_orch, store):
    orch, gw, note = make_orch({"card": [card("C", evidence=False)], "solo": [result(url=None)],
                                "critic": [verdict()]})
    tid = orch.submit("Напомни купить воду")
    await drive(orch)
    t = store.task(tid)
    assert t["status"] == "done" and t["cls"] == "C"
    assert gw.roles() == ["card", "solo", "critic"]
    assert note.last[0].startswith(f"✅ {tid}")


async def test_critic_is_other_vendor_than_author(make_orch, store):
    orch, gw, _ = make_orch({"card": [card("B")], "planner": [PLAN], "worker": [FINDING],
                             "synth": [result()], "critic": [verdict()]})
    orch.submit("Сводка новостей про AI-агентов")
    await drive(orch)
    critic_call = [c for c in gw.calls if c["role"] == "critic"][0]
    assert critic_call["exclude"] == ("anthropic",)  # автор synth — anthropic


async def test_team_runs_planned_subtasks_in_parallel(make_orch, store):
    orch, gw, _ = make_orch({"card": [card("B")], "planner": [PLAN], "worker": [FINDING],
                             "synth": [result()], "critic": [verdict()]})
    tid = orch.submit("Сравни три VPS")
    await drive(orch)
    assert gw.roles().count("worker") == 2
    assert store.task(tid)["status"] == "done"


async def test_class_a_always_waits_for_owner_and_journals_forecast(make_orch, store):
    orch, gw, note = make_orch({
        "card": [card("B", forecast="Перевод пройдёт без блокировки до 30.11?")],
        "planner": [PLAN], "worker": [FINDING],
        "synth": [result(actions=["Подтвердить перевод в кошельке"], p=0.91)],
        "critic": [verdict()]})
    tid = orch.submit("Вывести 800 USDT в баты")  # правила поднимают класс до A
    await drive(orch)
    t = store.task(tid)
    assert t["status"] == "awaiting_owner" and t["cls"] == "A"
    text, buttons = note.last
    assert "Что делаешь ты" in text and buttons
    approval_id = int(buttons[0][1].split(":")[1])
    reply = await orch.decide(approval_id, "accept")
    assert "принято" in reply
    assert store.task(tid)["status"] == "done"
    assert orch.journal.entries()[0]["p"] == 0.91


async def test_rework_twice_then_escalate(make_orch, store):
    orch, gw, note = make_orch({"card": [card("C", evidence=False)], "solo": [result(url=None)],
                                "critic": [verdict(False, "major", ["Нет сравнения вариантов"])]})
    tid = orch.submit("Напомни про созвон")
    await drive(orch, ticks=20)
    t = store.task(tid)
    assert t["status"] == "awaiting_owner"
    assert gw.roles().count("solo") == 3  # исходный + 2 доработки
    assert "после 2 кругов" in note.last[0]
    assert orch.memory.lessons("candidate")  # причины доработки → кандидаты в плейбук
    # замечания передаются исполнителю при доработке
    solo_calls = [c for c in gw.calls if c["role"] == "solo"]
    assert "Нет сравнения вариантов" in solo_calls[1]["messages"][1]["content"]


async def test_owner_grants_more_rounds(make_orch, store):
    orch, gw, note = make_orch({"card": [card("C", evidence=False)], "solo": [result(url=None)],
                                "critic": [verdict(False, "major", ["x"]), verdict(False, "major", ["x"]),
                                           verdict(False, "major", ["x"]), verdict()]})
    tid = orch.submit("Напомни про созвон")
    await drive(orch, ticks=20)
    approval_id = int(note.last[1][1][1].split(":")[1])  # кнопка «Ещё 2 круга»
    await orch.decide(approval_id, "rework")
    await drive(orch)
    assert store.task(tid)["status"] == "done"


async def test_clarification_flow(make_orch, store):
    orch, gw, note = make_orch({"card": [card("B", questions=["Какой бюджет?"]), card("B")],
                                "planner": [PLAN], "worker": [FINDING], "synth": [result()],
                                "critic": [verdict()]})
    tid = orch.submit("Подбери ноутбук")
    await drive(orch)
    assert store.task(tid)["status"] == "awaiting_clarification"
    assert "Какой бюджет?" in note.last[0]
    assert orch.answer_clarification(tid, "До 1500 долларов")
    await drive(orch)
    assert store.task(tid)["status"] == "done"
    card_calls = [c for c in gw.calls if c["role"] == "card"]
    assert "1500" in card_calls[1]["messages"][1]["content"]


async def test_stop_halts_everything(make_orch, store):
    orch, gw, _ = make_orch({"card": [card("C")]})
    orch.guard.stop()
    tid = orch.submit("Напомни купить воду")
    assert await orch.run_pending() == 0
    assert store.task(tid)["status"] == "new" and gw.calls == []


async def test_task_budget_exceeded_asks_owner_then_resumes(make_orch, store):
    orch, gw, note = make_orch({"card": [card("C", evidence=False)],
                                "solo": [BudgetExceeded("задача T-0001", 0.02, 0.02), result(url=None)],
                                "critic": [verdict()]})
    tid = orch.submit("Напомни купить воду")
    await drive(orch)
    assert store.task(tid)["status"] == "awaiting_owner"
    approval_id = int(note.last[1][0][1].split(":")[1])
    await orch.decide(approval_id, "extend")
    assert store.task(tid)["cap_multiplier"] == 2.0
    await drive(orch)
    assert store.task(tid)["status"] == "done"


async def test_day_budget_stops_system(make_orch, store):
    orch, gw, note = make_orch({"card": [BudgetExceeded("день", 10, 10)]})
    orch.submit("Сводка новостей")
    await drive(orch)
    assert orch.guard.stopped()
    assert "паузе" in note.last[0]


async def test_provider_paused_keeps_tasks(make_orch, store):
    orch, gw, note = make_orch({"card": [ProviderPaused("402")]})
    tid = orch.submit("Сводка новостей")
    await drive(orch)
    assert store.task(tid)["status"] == "new"
    assert "/resume_models" in note.last[0]


async def test_resume_after_restart(make_orch, settings, store):
    orch, gw, _ = make_orch({"card": [card("C", evidence=False)], "solo": [result(url=None)],
                             "critic": [verdict()]})
    tid = orch.submit("Напомни купить воду")
    await orch.advance(tid)  # только карточка
    store.update_task(tid, status="executing")  # «упали» посреди исполнения
    orch2, gw2, _ = make_orch({"solo": [result(url=None)], "critic": [verdict()]})
    await drive(orch2)
    assert store.task(tid)["status"] == "done"


async def test_local_only_without_local_model_asks_owner(make_orch, store):
    orch, gw, note = make_orch({"card": [card("B", privacy="local_only")], "planner": [PLAN],
                                "worker": [FINDING], "synth": [result()], "critic": [verdict()]})
    tid = orch.submit("Разбери мои медицинские анализы")
    await drive(orch)
    assert store.task(tid)["status"] == "awaiting_owner"
    approval_id = int(note.last[1][0][1].split(":")[1])
    await orch.decide(approval_id, "cloud")
    await drive(orch)
    assert store.task(tid)["status"] == "done"


async def test_injection_on_page_is_flagged_not_followed(make_orch, store, settings):
    class FakeSearcher:
        async def search(self, q, n=3):
            return ["https://evil.example/page"]

    class FakeReader:
        async def read(self, url, question, task_id=None, task_cap=None):
            return Evidence(url=url, tier=3, facts=[ExtractedFact(text="Комиссия 1%")],
                            instructions_detected=True)

    orch, gw, note = make_orch({"card": [card("B")], "planner": [PLAN], "worker": [FINDING],
                                "synth": [result()], "critic": [verdict()]},
                               reader=FakeReader(), searcher=FakeSearcher())
    tid = orch.submit("Сравни комиссии обменников")
    await drive(orch)
    assert store.task(tid)["status"] == "done"
    text = note.last[0]
    assert "инструкции для агента" in text
    worker_msg = [c for c in gw.calls if c["role"] == "worker"][0]["messages"][1]["content"]
    assert "Комиссия 1%" in worker_msg  # данные дошли, сырой страницы нет


async def test_cancel_by_owner(make_orch, store):
    orch, gw, note = make_orch({"card": [card("A")], "planner": [PLAN], "worker": [FINDING],
                                "synth": [result(actions=["Сделать перевод"])], "critic": [verdict()]})
    tid = orch.submit("Купить билеты")
    await drive(orch)
    approval_id = int(note.last[1][2][1].split(":")[1])
    await orch.decide(approval_id, "reject")
    assert store.task(tid)["status"] == "cancelled"
    assert await orch.decide(approval_id, "accept") == "Это решение уже принято или не найдено."


async def test_rules_never_lower_class(make_orch, store):
    orch, gw, _ = make_orch({"card": [card("C")], "planner": [PLAN], "worker": [FINDING],
                             "synth": [result(actions=["x"])], "critic": [verdict()]})
    tid = orch.submit("Перевести деньги маме")
    await orch.advance(tid)
    assert store.task(tid)["cls"] == TaskClass.A.value
