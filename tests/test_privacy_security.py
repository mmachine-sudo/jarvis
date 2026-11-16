"""Приватность и безопасность: локальные задачи, внутренняя сеть, секреты в логах."""
from __future__ import annotations

import logging

import pytest

from jarvis.guard import SecretLogFilter, is_public_url
from jarvis.reader import QuarantineReader

from .conftest import FINDING, PLAN, ScriptedGateway, card, result, verdict

BOT_TOKEN = "123456789:AAEhBP0av28mL3fKk1t5x8y9z0abcdefghi"


async def test_sensitive_request_sends_nothing_to_cloud_without_owner(make_orch, store):
    orch, gw, note = make_orch({"card": [card("B")], "planner": [PLAN], "worker": [FINDING],
                                "synth": [result(actions=["Отправить анкету самому"])], "critic": [verdict()]})
    tid = orch.submit("Вот мои паспортные данные, заполни анкету")  # «паспорт» → класс A
    await orch.run_pending()
    assert store.task(tid)["status"] == "awaiting_owner"
    assert gw.calls == []  # ни одного вызова облачной модели
    assert "ничего не отправлял" in note.last[0]
    approval_id = int(note.last[1][0][1].split(":")[1])
    await orch.decide(approval_id, "cloud")
    for _ in range(5):
        await orch.run_pending()
    t = store.task(tid)
    assert gw.roles()[0] == "card" and "critic" in gw.roles()
    assert t["status"] == "awaiting_owner" and t["cls"] == "A"  # класс A всегда ждёт решения владельца


async def test_local_model_handles_everything_for_private_task(make_orch, settings, store):
    settings.local_llm_url = "http://100.64.0.2:11434/v1"
    settings.models["local"] = ["local/qwen3"]
    orch, gw, note = make_orch({"local": [card("B", privacy="local_only"), PLAN, FINDING, FINDING,
                                          result(), verdict()]})
    tid = orch.submit("Разбери мои медицинские анализы")
    for _ in range(5):
        await orch.run_pending()
    assert store.task(tid)["status"] == "done"
    assert set(gw.roles()) == {"local"}  # в облако не ушло ничего


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:8090/api", False), ("http://10.0.0.5/", False), ("http://169.254.169.254/latest", False),
    ("file:///etc/passwd", False), ("http://[::1]/", False), ("https://8.8.8.8/", True),
])
def test_is_public_url(url, ok):
    assert is_public_url(url) is ok


async def test_reader_refuses_internal_address(settings):
    reader = QuarantineReader(settings, ScriptedGateway({}))
    with pytest.raises(PermissionError):
        await reader.fetch("http://127.0.0.1:8090/api/order")


def test_secret_log_filter_scrubs_token_in_message_and_traceback():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "url https://api.telegram.org/bot%s/getMe",
                               (BOT_TOKEN,), None)
    try:
        raise RuntimeError(f"failed https://api.telegram.org/bot{BOT_TOKEN}/sendMessage")
    except RuntimeError:
        import sys
        record.exc_info = sys.exc_info()
    SecretLogFilter().filter(record)
    assert BOT_TOKEN not in record.getMessage()
    assert BOT_TOKEN not in (record.exc_text or "")
