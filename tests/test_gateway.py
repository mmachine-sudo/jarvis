"""Шлюз моделей. Несколько тестов — прямые уроки инцидентов MMachine."""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from jarvis.config import Settings
from jarvis.gateway import AllCandidatesFailed, ModelGateway, ProviderPaused, extract_json
from jarvis.store import Store
from jarvis.treasury import BudgetExceeded, Treasury


class Out(BaseModel):
    ok: bool


def ok_body(content: str, cost: float | None = 0.003) -> dict:
    body = {"choices": [{"message": {"content": content}}]}
    if cost is not None:
        body["usage"] = {"cost": cost}
    return body


def make(handler, models=None, day_usd=10.0):
    s = Settings()
    s.openrouter_api_key = "test"
    s.models = models or {"solo": ["anthropic/a", "openai/b"]}
    s.budget.day_usd = day_usd
    store = Store(":memory:")
    t = Treasury(store, s.budget)
    gw = ModelGateway(s, t, store, transport=httpx.MockTransport(handler))
    return gw, store, t


def msgs():
    return [{"role": "user", "content": "привет"}]


async def test_success_records_cost():
    gw, store, t = make(lambda r: httpx.Response(200, json=ok_body('{"ok": true}', 0.004)))
    reply = await gw.chat("solo", msgs(), task_id="T-1", schema=Out)
    assert reply.data.ok and reply.model == "anthropic/a" and reply.vendor == "anthropic"
    assert t.spent_task("T-1") == pytest.approx(0.004)


async def test_lesson_402_pauses_all_calls():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(402, json={"error": "insufficient credits"})

    gw, store, _ = make(handler)
    with pytest.raises(ProviderPaused):
        await gw.chat("solo", msgs())
    assert gw.is_paused()
    with pytest.raises(ProviderPaused):
        await gw.chat("solo", msgs())
    assert len(calls) == 1  # второй вызов не ушёл в сеть
    gw.resume_provider()
    assert not gw.is_paused()


async def test_lesson_empty_answer_retries_with_4x_tokens():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body["max_tokens"])
        return httpx.Response(200, json=ok_body("" if len(seen) == 1 else '{"ok": true}'))

    gw, *_ = make(handler)
    reply = await gw.chat("solo", msgs(), max_tokens=500, schema=Out)
    assert seen == [500, 2000] and reply.data.ok


async def test_lesson_broken_json_goes_to_next_candidate():
    def handler(req):
        model = json.loads(req.content)["model"]
        if model == "anthropic/a":
            return httpx.Response(200, json=ok_body("<think>…</think> ok: да"))
        return httpx.Response(200, json=ok_body('```json\n{"ok": true}\n```'))

    gw, *_ = make(handler)
    reply = await gw.chat("solo", msgs(), schema=Out)
    assert reply.model == "openai/b" and reply.data.ok


async def test_exclude_vendor():
    gw, *_ = make(lambda r: httpx.Response(200, json=ok_body('{"ok": true}')))
    reply = await gw.chat("solo", msgs(), schema=Out, exclude_vendors=("anthropic",))
    assert reply.vendor == "openai"
    with pytest.raises(AllCandidatesFailed):
        await gw.chat("solo", msgs(), exclude_vendors=("anthropic", "openai"))


async def test_server_errors_fall_through_then_fail():
    gw, *_ = make(lambda r: httpx.Response(503))
    with pytest.raises(AllCandidatesFailed) as e:
        await gw.chat("solo", msgs())
    assert "anthropic/a: HTTP 503" in str(e.value) and "openai/b: HTTP 503" in str(e.value)


async def test_budget_checked_before_network():
    calls = []
    gw, store, t = make(lambda r: calls.append(r) or httpx.Response(200, json=ok_body("x")), day_usd=0.01)
    t.record(None, "m", 0.01)
    with pytest.raises(BudgetExceeded):
        await gw.chat("solo", msgs())
    assert calls == []


async def test_missing_cost_is_counted_conservatively():
    gw, store, t = make(lambda r: httpx.Response(200, json=ok_body("текст", cost=None)))
    await gw.chat("solo", msgs(), task_id="T-2")
    assert t.spent_task("T-2") == pytest.approx(gw.settings.budget.est_call_usd)


def test_extract_json():
    assert extract_json('вот: {"a": {"b": 1}} конец') == '{"a": {"b": 1}}'
    with pytest.raises(ValueError):
        extract_json("без json")
