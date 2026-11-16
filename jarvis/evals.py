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


async def run_online(app, cases: list[dict], max_ticks: int = 30) -> tuple[int, int, float, list[str]]:
    """Прогон через настоящий оркестратор. Задачи, ждущие владельца, считаются дошедшими до решения."""
    report, passed = [], 0
    start = app.treasury.spent_month()
    for c in cases:
        tid = app.orch.submit(c["request"])
        for _ in range(max_ticks):
            t = app.store.task(tid)
            if t["status"] == "awaiting_clarification":
                app.orch.answer_clarification(tid, c.get("clarify_answer", "Действуй на своё усмотрение."))
            if t["status"] in ("done", "awaiting_owner", "cancelled", "failed"):
                break
            await app.orch.advance(tid)
        t = app.store.task(tid)
        expect = c.get("expect_final", ["done", "awaiting_owner"])
        ok = t["status"] in expect and (c.get("expect_class") in (None, t["cls"]))
        passed += ok
        report.append(f"{'✓' if ok else '✗'} {c['id']}: {t['status']} · класс {t['cls']} · {t['note'] or ''}")
    return passed, len(cases), app.treasury.spent_month() - start, report
