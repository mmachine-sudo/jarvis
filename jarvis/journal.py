"""Журнал решений: прогнозы с датой проверки и Brier score — сигнал истины для самообучения."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .store import Store, now_iso


class Journal:
    def __init__(self, store: Store):
        self.store = store

    def add(self, task_id: str, question: str, p: float, resolve_in_days: int = 30,
            resolve_by: str | None = None) -> int:
        p = min(max(float(p), 0.0), 1.0)
        by = resolve_by or (date.today() + timedelta(days=resolve_in_days)).isoformat()
        cur = self.store.x(
            "INSERT INTO journal(task_id,question,p,resolve_by,created_at) VALUES(?,?,?,?,?)",
            (task_id, question, p, by, now_iso()))
        return int(cur.lastrowid)

    def due(self, today: str | None = None) -> list[dict]:
        today = today or date.today().isoformat()
        return self.store.all(
            "SELECT * FROM journal WHERE outcome IS NULL AND resolve_by <= ? AND asked_at IS NULL ORDER BY id",
            (today,))

    def mark_asked(self, entry_id: int) -> None:
        self.store.x("UPDATE journal SET asked_at=? WHERE id=?", (now_iso(), entry_id))

    def resolve(self, entry_id: int, happened: bool) -> None:
        self.store.x("UPDATE journal SET outcome=?, resolved_at=? WHERE id=?",
                     (1 if happened else 0, now_iso(), entry_id))

    def entries(self, resolved: bool | None = None) -> list[dict]:
        if resolved is None:
            return self.store.all("SELECT * FROM journal ORDER BY id DESC")
        op = "IS NOT NULL" if resolved else "IS NULL"
        return self.store.all(f"SELECT * FROM journal WHERE outcome {op} ORDER BY id DESC")

    def brier(self, days: int | None = None) -> tuple[float | None, int]:
        rows = self.entries(resolved=True)
        if days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            rows = [r for r in rows if (r["resolved_at"] or "") >= cutoff]
        if not rows:
            return None, 0
        score = sum((r["p"] - r["outcome"]) ** 2 for r in rows) / len(rows)
        return round(score, 4), len(rows)

    def calibration(self, bins: int = 5) -> list[dict]:
        rows = self.entries(resolved=True)
        out = []
        for i in range(bins):
            lo, hi = i / bins, (i + 1) / bins
            group = [r for r in rows if lo <= r["p"] < hi or (i == bins - 1 and r["p"] == 1.0)]
            if group:
                out.append({"диапазон": f"{lo:.1f}–{hi:.1f}", "прогнозов": len(group),
                            "средняя_p": round(sum(r["p"] for r in group) / len(group), 2),
                            "сбылось": round(sum(r["outcome"] for r in group) / len(group), 2)})
        return out
