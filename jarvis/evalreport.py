"""Разбор прогона эталонов по базе evals.db (бесплатно: только чтение). Куда ушли деньги и время."""
from __future__ import annotations

import sqlite3


def report(path: str) -> list[str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = []
    for t in con.execute("SELECT id, status, cls, round, note, request, issues_json FROM tasks ORDER BY created_at"):
        steps = con.execute("SELECT kind, actor, model, cost_usd, detail, created_at FROM steps WHERE task_id=? "
                            "ORDER BY id", (t["id"],)).fetchall()
        usd = sum(s["cost_usd"] or 0 for s in steps)
        spent = con.execute("SELECT COALESCE(SUM(usd),0) s, COUNT(*) n FROM spend WHERE task_id=?", (t["id"],)).fetchone()
        first = steps[0]["created_at"] if steps else "?"
        last = steps[-1]["created_at"] if steps else "?"
        out.append(f"{t['id']} [{t['status']}, класс {t['cls']}, круг {t['round']}] шагов {len(steps)}, "
                   f"по шагам ${usd:.3f}, по счётчику ${spent['s']:.3f} за {spent['n']} вызовов, {first[11:19]}–{last[11:19]} · "
                   f"{(t['request'] or '')[:50]}")
        kinds: dict = {}
        for s in steps:
            k = f"{s['kind']}/{s['actor']}"
            n, c = kinds.get(k, (0, 0.0))
            kinds[k] = (n + 1, c + (s["cost_usd"] or 0))
        out.append("   " + "; ".join(f"{k}×{n} ${c:.3f}" for k, (n, c) in kinds.items()))
        if t["issues_json"] and t["status"] != "done":
            out.append("   замечания: " + str(t["issues_json"])[:600])
        if t["cls"] == "A":  # таймлайн: где уходит время
            line = []
            for s in steps:
                ts = s["created_at"][11:19]
                line.append(f"{ts} {s['kind']}/{(s['model'] or s['actor'])[:18]}")
            out.append("   таймлайн: " + " → ".join(line)[:1500])
        for s in steps[-4:]:
            out.append(f"   …{s['created_at'][11:19]} {s['kind']}/{s['actor']} {s['model'] or ''}: {(s['detail'] or '')[:160]}")
    by_model = con.execute("SELECT model, COUNT(*) n, ROUND(SUM(usd),3) usd FROM spend GROUP BY model ORDER BY usd DESC")
    out.append("Траты по моделям: " + "; ".join(f"{r['model']} {r['n']} выз. ${r['usd']}" for r in by_model))
    con.close()
    return out
