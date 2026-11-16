"""Эталонный набор: офлайн-проверка роутера и онлайн-прогон задач целиком.

Офлайн (`jarvis evals`) — бесплатно: класс и режим по правилам.
Онлайн (`jarvis evals --online`) — настоящие модели: карточка, исполнение, проверка;
считает долю принятых и стоимость. Ворота фазы 1: ≥ 80% эталонных задач проходят.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .card import MODE_BY_CLASS
from .config import Settings
from .router import classify


def load_cases(path: str | Path = "evals/cases.yaml") -> list[dict]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))["cases"]


def run_offline(settings: Settings, cases: list[dict]) -> tuple[int, int, list[str]]:
    passed, report = 0, []
    for c in cases:
        cls = classify(c["request"], settings.router)
        mode = MODE_BY_CLASS[cls].value
        exp_cls, exp_mode = c.get("expect_class"), c.get("expect_mode")
        ok = (exp_cls is None or cls.value == exp_cls) and (exp_mode is None or mode == exp_mode)
        passed += ok
        report.append(f"{'✓' if ok else '✗'} {c['id']}: класс {cls.value} (ждали {exp_cls}), режим {mode}")
    return passed, len(cases), report


async def run_online(app, cases: list[dict], max_ticks: int = 30, max_usd: float | None = None,
                     case_timeout: float = 900.0, on_line=None) -> tuple[int, int, float, list[str]]:
    """Прогон через настоящий оркестратор. Задачи, ждущие владельца, считаются дошедшими до решения.
    Каждая строка итога отдаётся сразу (on_line), чтобы прогон был виден, даже если его прервут."""
    import asyncio
    report, passed = [], 0
    start = app.treasury.spent_month()

    def emit(line: str) -> None:
        report.append(line)
        if on_line:
            on_line(line)

    async def drive(c: dict, tid: str) -> None:
        for _ in range(max_ticks):
            t = app.store.task(tid)
            if t["status"] == "awaiting_clarification":
                app.orch.answer_clarification(tid, c.get("clarify_answer", "Действуй на своё усмотрение."))
            if t["status"] in ("done", "awaiting_owner", "cancelled", "failed"):
                return
            if await app.orch.step(tid) is None:  # пауза провайдера
                return

    for c in cases:
        if max_usd is not None and app.treasury.spent_month() - start >= max_usd:
            emit(f"■ остановлено: потолок прогона ${max_usd:.2f}")
            break
        tid = app.orch.submit(c["request"])
        timed_out = False
        try:
            await asyncio.wait_for(drive(c, tid), timeout=case_timeout)
        except asyncio.TimeoutError:
            timed_out = True
        t = app.store.task(tid)
        spent = sum(x["cost_usd"] or 0 for x in app.store.steps(tid))
        expect = c.get("expect_final", ["done", "awaiting_owner"])
        # эскалация «не прошло проверку после доработок» — не успех, даже если статус awaiting_owner
        escalated = any(x in (t["note"] or "") for x in ("не прошло проверку", "исчерпан"))  # и упор в лимит
        ok = (not timed_out and not escalated and t["status"] in expect
              and (c.get("expect_class") in (None, t["cls"])))
        passed += ok
        why = f"не уложился в {case_timeout:.0f} с" if timed_out else (t["note"] or "")[:80]
        emit(f"{'✓' if ok else '✗'} {c['id']}: {t['status']} · класс {t['cls']} · ${spent:.3f} · {why}")
    return passed, len(cases), app.treasury.spent_month() - start, report
