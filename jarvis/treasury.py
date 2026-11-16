"""Казначей: лимиты на день, месяц и задачу; учёт фактических трат."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import BudgetCfg
from .store import Store, now_iso


class BudgetExceeded(Exception):
    def __init__(self, scope: str, spent: float, cap: float):
        self.scope, self.spent, self.cap = scope, spent, cap
        super().__init__(f"Лимит «{scope}» исчерпан: ${spent:.2f} из ${cap:.2f}")


class Treasury:
    def __init__(self, store: Store, cfg: BudgetCfg, tz: str = "UTC", clock=None):
        self.store, self.cfg = store, cfg
        self.tz = ZoneInfo(tz)
        self._clock = clock or (lambda: datetime.now(self.tz))

    def _today(self) -> tuple[str, str]:
        d = self._clock()
        return d.strftime("%Y-%m-%d"), d.strftime("%Y-%m")

    def spent_day(self) -> float:
        day, _ = self._today()
        row = self.store.one("SELECT COALESCE(SUM(usd),0) s FROM spend WHERE day=?", (day,))
        return float(row["s"])

    def spent_month(self) -> float:
        _, month = self._today()
        row = self.store.one("SELECT COALESCE(SUM(usd),0) s FROM spend WHERE month=?", (month,))
        return float(row["s"])

    def spent_task(self, task_id: str | None) -> float:
        if not task_id:
            return 0.0
        row = self.store.one("SELECT COALESCE(SUM(usd),0) s FROM spend WHERE task_id=?", (task_id,))
        return float(row["s"])

    def check(self, task_id: str | None, est_usd: float, task_cap: float | None = None) -> None:
        """Бросает BudgetExceeded, если следующий вызов выйдет за любой лимит."""
        day, month = self.spent_day(), self.spent_month()
        if day + est_usd > self.cfg.day_usd:
            raise BudgetExceeded("день", day, self.cfg.day_usd)
        if month + est_usd > self.cfg.month_usd:
            raise BudgetExceeded("месяц", month, self.cfg.month_usd)
        if task_cap is not None and task_id:
            spent = self.spent_task(task_id)
            # оценка вызова не больше четверти лимита задачи: иначе задача класса C ($0.02) не могла сделать
            # ни одного вызова после первого (эталоны 16.11: «исчерпан $0.00 из $0.02»)
            if spent + min(est_usd, task_cap / 4) > task_cap:
                raise BudgetExceeded(f"задача {task_id}", spent, task_cap)

    def record(self, task_id: str | None, model: str, usd: float) -> None:
        day, month = self._today()
        self.store.x("INSERT INTO spend(ts,day,month,task_id,model,usd) VALUES(?,?,?,?,?,?)",
                     (now_iso(), day, month, task_id, model, float(usd)))

    def by_model(self, days: int = 30) -> list[dict]:
        return self.store.all(
            "SELECT model, ROUND(SUM(usd),4) usd, COUNT(*) calls FROM spend "
            "WHERE ts >= datetime('now', ?) GROUP BY model ORDER BY usd DESC", (f"-{days} days",))
