"""CLI: jarvis run | doctor | evals [--online] | status."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_settings


class _Silent:
    async def notify(self, text, buttons=None):
        return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jarvis")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="запустить бота и воркер")
    sub.add_parser("doctor", help="проверить секреты, модели, Telegram, базу")
    ev = sub.add_parser("evals", help="эталонный набор")
    ev.add_argument("--online", action="store_true", help="прогнать через настоящие модели (тратит бюджет)")
    ev.add_argument("--cases", default="evals/cases.yaml")
    ev.add_argument("--max-usd", type=float, default=3.0, help="потолок трат прогона")
    sub.add_parser("status", help="бюджет и задачи")
    inh = sub.add_parser("inherit-report", help="агрегаты из снимка базы MMachine (модели, доработки)")
    inh.add_argument("path", nargs="?", default="/var/lib/jarvis/inherit/mmachine.db")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .guard import install_log_redaction
    install_log_redaction()
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx пишет URL запроса, а в URL Telegram — токен
    if args.cmd == "inherit-report":
        from .inherit import analyze, report_lines
        print("\n".join(report_lines(analyze(args.path))))
        return 0
    settings = load_settings(args.config)

    if args.cmd == "doctor":
        from .doctor import check
        ok, lines = check(settings)
        print("\n".join(lines))
        print("\nГотово к запуску." if ok else "\nЕсть проблемы — исправь пункты с ✗.")
        return 0 if ok else 1

    if args.cmd == "evals":
        from .evals import load_cases, run_offline, run_online
        cases = load_cases(args.cases)
        if not args.online:
            passed, total, report = run_offline(settings, cases)
            print("\n".join(report))
            print(f"\nРоутер: {passed}/{total}")
            return 0 if passed == total else 1
        from pathlib import Path
        from .app import build
        # отдельная база: эталоны не попадают в задачи, траты, журнал и плейбук владельца
        settings.db_path = str(Path(settings.db_path).with_name("evals.db"))
        prior = 0.0  # траты прерванного прошлого прогона входят в тот же потолок
        if Path(settings.db_path).exists():
            import sqlite3
            con = sqlite3.connect(settings.db_path)
            try:
                prior = float(con.execute("SELECT COALESCE(SUM(usd),0) FROM spend").fetchone()[0])
            except sqlite3.Error:
                pass
            con.close()
            for suffix in ("", "-wal", "-shm"):
                Path(settings.db_path + suffix).unlink(missing_ok=True)
        if prior:
            print(f"■ прошлый прогон успел потратить ${prior:.2f}", flush=True)
        app = build(settings)
        app.orch.notifier = _Silent()  # не писать владельцу в Telegram во время прогона
        online = [c for c in cases if c.get("online", True)]
        passed, total, usd, report = asyncio.run(run_online(
            app, online, max_usd=max(args.max_usd - prior, 0.0), on_line=lambda line: print(line, flush=True)))
        print(f"\nПрошло {passed}/{total} · потрачено ${usd + prior:.2f} · ворота фазы 1: ≥ 80%", flush=True)
        return 0 if total and passed / total >= 0.8 else 1

    if args.cmd == "status":
        from .store import Store
        from .treasury import Treasury
        store = Store(settings.db_path)
        t = Treasury(store, settings.budget, settings.owner.timezone)
        print(f"Сегодня ${t.spent_day():.2f} / ${settings.budget.day_usd}, месяц ${t.spent_month():.2f} / ${settings.budget.month_usd}")
        for r in store.tasks(limit=10):
            print(f"{r['id']} {r['status']:<22} {r['cls'] or '?'} {r['request'][:60]}")
        return 0

    if args.cmd == "run":
        from .app import run
        try:
            asyncio.run(run(settings))
        except KeyboardInterrupt:
            pass
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
