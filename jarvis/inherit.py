"""Наследство MMachine: что из её истории полезно Jarvis (только чтение снимка базы).

Главное — надёжность моделей на реальной работе: доля успешных вызовов, типы сбоев, задержка, цена вызова.
Эти цифры задают порядок кандидатов в ролях Jarvis вместо «на глаз». Плюс цена доработок по видам задач.
Тексты задач и ответов наружу не выводятся (там могли быть данные покупателей) — только агрегаты.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict

from .guard import scrub_secrets

_ERR = [("402/баланс", r"402|insufficient|credit"), ("429/лимит", r"429|rate.?limit"),
        ("таймаут", r"timeout|timed out|ReadTimeout"), ("5xx", r"\b5\d\d\b|server error|overloaded"),
        ("пустой ответ", r"empty|пуст"), ("формат/JSON", r"json|format|формат"),
        ("нет модели", r"404|not found|no endpoints|invalid model")]


def _err_kind(text: str) -> str:
    for name, pat in _ERR:
        if re.search(pat, text or "", re.I):
            return name
    return "другое"


def _has(con: sqlite3.Connection, table: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def analyze(path: str, days: int = 60) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out: dict = {"models": [], "rework": [], "tasks": {}}
    if _has(con, "calls"):
        last = con.execute("SELECT MAX(ts) m FROM calls").fetchone()["m"]
        rows = con.execute("SELECT model, ok, error, ms, cost_usd FROM calls WHERE ts >= date(?, ?)",
                           (last, f"-{days} day")).fetchall() if last else []
        agg: dict = defaultdict(lambda: {"n": 0, "ok": 0, "ms": [], "usd": 0.0, "err": Counter()})
        for r in rows:
            a = agg[r["model"] or "?"]
            a["n"] += 1
            a["usd"] += r["cost_usd"] or 0
            if r["ok"]:
                a["ok"] += 1
                if r["ms"]:
                    a["ms"].append(r["ms"])
            else:
                a["err"][_err_kind(r["error"] or "")] += 1
        for model, a in agg.items():
            ms = sorted(a["ms"])
            out["models"].append({"model": model, "n": a["n"], "ok_rate": round(a["ok"] / a["n"], 3),
                                  "p50_s": round(ms[len(ms) // 2] / 1000, 1) if ms else None,
                                  "usd_per_call": round(a["usd"] / a["n"], 5), "errors": dict(a["err"].most_common(3))})
        out["models"].sort(key=lambda m: -m["n"])
        out["calls_period"] = f"{days} дней до {last[:10]}" if last else "нет вызовов"
    if _has(con, "rework"):
        out["rework"] = [dict(r) for r in con.execute(
            "SELECT kind, COUNT(*) n, ROUND(SUM(cost),3) usd FROM rework GROUP BY kind ORDER BY usd DESC LIMIT 8")]
    if _has(con, "tasks"):
        out["tasks"] = {f"{r['status']}": r["n"] for r in con.execute(
            "SELECT status, COUNT(*) n FROM tasks GROUP BY status ORDER BY n DESC")}
    con.close()
    return out


def report_lines(data: dict, min_calls: int = 20) -> list[str]:
    lines = [f"Наследство MMachine — модели ({data.get('calls_period', '?')}):"]
    for m in data["models"]:
        if m["n"] < min_calls:
            continue
        err = ", ".join(f"{k} {v}" for k, v in m["errors"].items()) or "—"
        lines.append(f"{m['model']}: {m['n']} вызовов, успешно {m['ok_rate']:.0%}, медиана {m['p50_s']} с, "
                     f"${m['usd_per_call']:.4f}/вызов, сбои: {err}")
    if data["rework"]:
        lines.append("Доработки MMachine по видам задач: " +
                     "; ".join(f"{r['kind']} {r['n']} кр. ${r['usd'] or 0}" for r in data["rework"]))
    if data["tasks"]:
        lines.append("Задачи MMachine по статусам: " + ", ".join(f"{k} {v}" for k, v in data["tasks"].items()))
    return [scrub_secrets(x) for x in lines]
