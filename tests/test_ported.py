"""Перенесённое из MMachine: разбор JSON, консилиум класса A, учёт и разбор доработок, калибровка прогнозов."""
import pytest

from jarvis.gateway import extract_json
from jarvis.journal import Journal
from jarvis.memory import Memory
from jarvis.rework import Rework
from jarvis.store import Store
from jarvis.verify import consilium_review

from .conftest import ScriptedGateway, card, result, verdict  # noqa: F401


def _card_a():
    from jarvis.card import TaskBudget, TaskCard, TaskClass
    return TaskCard(id="T-1", goal="Цель", result_image="Ответ", cls=TaskClass.A, budget=TaskBudget(usd_cap=1.0))


def test_extract_json_lessons_from_mmachine():
    assert extract_json('Вот: {"a": 1} и ещё {"b": 2} — готово') == '{"a": 1}'
    assert extract_json('```json\n{"a": [1, 2,],}\n```') == '{"a": [1, 2]}'
    with pytest.raises(ValueError, match="оборван"):
        extract_json('{"a": 1, "b": "не закры')


async def test_consilium_needs_majority_and_no_blocking(settings):
    from jarvis.card import Result
    c, r = _card_a(), Result.model_validate(result())
    ScriptedGateway.MODELS = dict(ScriptedGateway.MODELS, critic=["openai/gpt-6-luna", "google/gemini-3.8-flash",
                                                                   "x-ai/grok-5"])
    try:
        ok = ScriptedGateway({"critic": [verdict(True), verdict(True), verdict(False, "minor", ["стиль"])]})
        v, replies = await consilium_review(c, r, "anthropic/claude-sonnet-5.5", gw=ok, settings=settings)
        assert v.passed and len(replies) == 3
        assert len({x["exclude"] for x in ok.calls}) == 3  # каждый следующий голос — другой вендор

        bad = ScriptedGateway({"critic": [verdict(True), verdict(True), verdict(False, "major", ["ошибка в сумме"])]})
        v, _ = await consilium_review(c, r, "anthropic/claude-sonnet-5.5", gw=bad, settings=settings)
        assert not v.passed and "ошибка в сумме" in v.issues
    finally:
        ScriptedGateway.MODELS = {"critic": ["openai/gpt-6-luna", "google/gemini-3.8-flash"],
                                  "worker": ["deepseek/deepseek-v4.1-flash"]}


async def test_consilium_does_not_pass_on_single_vote(settings):
    from jarvis.card import Result
    c, r = _card_a(), Result.model_validate(result())
    gw = ScriptedGateway({"critic": [verdict(True)]})
    # автор — openai, у критика остаётся только google: один голос → не принято
    v, replies = await consilium_review(c, r, "openai/gpt-6-luna", gw=gw, settings=settings)
    assert len(replies) == 1 and not v.passed and any("не собрался" in i for i in v.issues)


async def test_rework_ledger_and_daily_review(store):
    rw, mem = Rework(store), Memory(store)
    store.x("INSERT INTO steps(task_id,kind,actor,detail,cost_usd,created_at) VALUES('T-1','x','y','z',0.04,'t')")
    assert rw.log_round("T-1", "team|B", 1, ["нет источника"]) == pytest.approx(0.04)
    store.x("INSERT INTO steps(task_id,kind,actor,detail,cost_usd,created_at) VALUES('T-1','x','y','z',0.03,'t')")
    assert rw.log_round("T-1", "team|B", 2, ["нет источника у цены"]) == pytest.approx(0.03)  # только новый круг
    gw = ScriptedGateway({"worker": [{"rules": ["У каждой цены — ссылка на источник", "У каждой цены — ссылка на источник"]}]})
    text = await rw.daily_review(gw, mem)
    assert "2 кругов" in text and "team|B" in text and "ссылка на источник" in text
    assert [x["text"] for x in mem.lessons("candidate")] == ["У каждой цены — ссылка на источник"]  # без дублей
    assert await Rework(Store(":memory:")).daily_review(gw, mem) is None


def test_journal_calibration_adjusts_overconfidence(store):
    j = Journal(store)
    assert j.adjust(0.9)["active"] is False  # мало истории — без поправки
    for i in range(12):
        eid = j.add(f"T-{i}", "q", 0.9)
        j.resolve(eid, happened=i < 6)  # говорили 90%, сбылось 50%
    a = j.adjust(0.9)
    assert a["active"] and 0.5 < a["p"] < 0.9


def test_inherit_report_aggregates_only(tmp_path):
    import sqlite3
    from jarvis.inherit import analyze, report_lines
    db = tmp_path / "mm.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE calls(id INTEGER PRIMARY KEY, ts TEXT, task_id INTEGER, role TEXT, provider TEXT,
      model TEXT, vendor TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, ok INTEGER, error TEXT, ms INTEGER);
      CREATE TABLE rework(id INTEGER PRIMARY KEY, ts TEXT, task_id INTEGER, kind TEXT, key TEXT, round INTEGER,
      cost REAL, reason TEXT, model TEXT);
      CREATE TABLE tasks(id INTEGER PRIMARY KEY, kind TEXT, status TEXT, payload TEXT);""")
    for i in range(30):
        con.execute("INSERT INTO calls(ts,model,cost_usd,ok,error,ms) VALUES(?,?,?,?,?,?)",
                    ("2026-11-09T10:00:00", "openai/x", 0.01, int(i % 10 != 0), "HTTP 429 rate limit", 2000))
    con.execute("INSERT INTO rework(ts,kind,key,round,cost,reason) VALUES('t','scout','k',1,0.2,'секрет покупателя')")
    con.execute("INSERT INTO tasks(kind,status,payload) VALUES('scout','done','покупатель Иванов')")
    con.commit(); con.close()
    text = "\n".join(report_lines(analyze(str(db))))
    assert "openai/x: 30 вызовов, успешно 90%" in text and "429/лимит 3" in text
    assert "scout 1 кр." in text and "Иванов" not in text and "покупателя" not in text


def test_finding_accepts_loose_model_output():
    from jarvis.modes import Finding
    f = Finding.model_validate({"finding": "x", "claims": ["факт без ссылки", {"text": "с ссылкой", "source_url": "https://a.go.th"}],
                                "confidence": "высокая"})
    assert [c.text for c in f.claims] == ["факт без ссылки", "с ссылкой"] and f.confidence == 0.8
    assert Finding.model_validate({"finding": "x", "confidence": "70%"}).confidence == 0.7


async def test_truncated_json_is_retried_with_more_tokens():
    import httpx, json as _json
    from pydantic import BaseModel
    from jarvis.config import Settings
    from jarvis.gateway import ModelGateway
    from jarvis.treasury import Treasury
    seen = []

    def handler(req):
        body = _json.loads(req.content)
        seen.append(body["max_tokens"])
        if len(seen) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": "обор'},
                                                          "finish_reason": "length"}], "usage": {"cost": 0.001}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": "ok"}'}}], "usage": {"cost": 0.001}})

    class A(BaseModel):
        a: str
    s = Settings(); s.openrouter_api_key = "t"
    store = Store()
    gw = ModelGateway(s, Treasury(store, s.budget), store, transport=httpx.MockTransport(handler))
    r = await gw.chat("worker", [{"role": "user", "content": "x"}], schema=A, max_tokens=500)
    assert r.data.a == "ok" and seen == [500, 1000]


async def test_team_survives_one_failed_subtask(make_orch, store):
    from jarvis.gateway import AllCandidatesFailed
    from .conftest import PLAN, FINDING
    orch, gw, note = make_orch({"card": [card("B")], "planner": [PLAN],
                                "worker": [AllCandidatesFailed("модели не ответили"), FINDING],
                                "synth": [result()], "critic": [verdict(True)]})
    tid = orch.submit("Сравни два варианта страховки")
    for _ in range(10):
        await orch.run_pending()
    assert store.task(tid)["status"] == "done"
    assert any("подзадача без ответа" in s["detail"] for s in store.steps(tid))
