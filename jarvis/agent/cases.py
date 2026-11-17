"""Библиотека дел — память об опыте, а не о фактах.

Дело = задача → какие стратегии рассматривал → какую выбрал и почему → что получилось → что сказал владелец →
какой реальный исход через N дней → уроки. Перед новой задачей агент ищет похожие дела (полнотекстовый поиск
с морфологически грубым стеммингом) и обязан написать, что сделает иначе. Так выполняется шаг 8 схемы владельца:
«в следующий раз — без прошлых ошибок и лучшей стратегией».
"""
from __future__ import annotations

import json
import re

from ..store import Store, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT UNIQUE, title TEXT, request TEXT,
  strategy TEXT, alternatives_json TEXT, result_summary TEXT, result_path TEXT, confidence REAL,
  owner_feedback TEXT, outcome TEXT, lessons_json TEXT, cost_usd REAL DEFAULT 0, status TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS cases_fts USING fts5(task_id UNINDEXED, body);
"""

_WORD = re.compile(r"[\w-]{3,}", re.U)
_STOP = {"для", "что", "как", "это", "или", "при", "под", "над", "все", "его", "она", "они", "the", "and", "with",
         "найди", "собери", "сделай", "нужно", "надо", "лучшие", "максимальн", "минимальн"}


def _stems(text: str) -> list[str]:
    """Грубый стемминг: первые 5 букв (русские окончания отрезаются), без стоп-слов."""
    out = []
    for w in _WORD.findall((text or "").lower()):
        s = w[:5] if len(w) > 5 else w
        if s not in _STOP and w not in _STOP:
            out.append(s)
    return out


class Cases:
    def __init__(self, store: Store):
        self.store = store
        store.conn.executescript(SCHEMA)

    def open(self, task_id: str, title: str, request: str) -> None:
        if self.store.one("SELECT id FROM cases WHERE task_id=?", (task_id,)):
            return
        ts = now_iso()
        self.store.x("INSERT INTO cases(task_id,title,request,status,created_at,updated_at,lessons_json) "
                     "VALUES(?,?,?,?,?,?,?)", (task_id, title[:200], request, "in_progress", ts, ts, "[]"))
        self._index(task_id)

    def update(self, task_id: str, **fields) -> None:
        if not fields:
            return
        for k in ("alternatives", "lessons"):
            if k in fields:
                fields[f"{k}_json"] = json.dumps(fields.pop(k), ensure_ascii=False)
        fields["updated_at"] = now_iso()
        cols = ",".join(f"{k}=?" for k in fields)
        self.store.x(f"UPDATE cases SET {cols} WHERE task_id=?", (*fields.values(), task_id))
        self._index(task_id)

    def add_lessons(self, task_id: str, lessons: list[str], source: str) -> None:
        c = self.get(task_id)
        if not c:
            return
        have = c["lessons"]
        for text in lessons:
            text = " ".join(str(text).split())[:400]
            if len(text) > 8 and all(text != x["text"] for x in have):
                have.append({"text": text, "source": source, "at": now_iso()})
        self.update(task_id, lessons=have[-20:])

    def add_cost(self, task_id: str, usd: float) -> None:
        self.store.x("UPDATE cases SET cost_usd=COALESCE(cost_usd,0)+? WHERE task_id=?", (usd, task_id))

    def get(self, task_id: str) -> dict | None:
        r = self.store.one("SELECT * FROM cases WHERE task_id=?", (task_id,))
        if not r:
            return None
        r = dict(r)
        r["lessons"] = Store.loads(r.pop("lessons_json"), []) or []
        r["alternatives"] = Store.loads(r.pop("alternatives_json"), []) or []
        return r

    def _index(self, task_id: str) -> None:
        c = self.store.one("SELECT * FROM cases WHERE task_id=?", (task_id,))
        lessons = " ".join(x.get("text", "") for x in (Store.loads(c["lessons_json"], []) or []))
        body = " ".join(_stems(" ".join(str(c[k] or "") for k in ("title", "request", "strategy", "result_summary",
                                                                     "owner_feedback", "outcome")) + " " + lessons))
        self.store.x("DELETE FROM cases_fts WHERE task_id=?", (task_id,))
        self.store.x("INSERT INTO cases_fts(task_id, body) VALUES(?,?)", (task_id, body))

    def similar(self, text: str, limit: int = 5, exclude: str | None = None) -> list[dict]:
        terms = list(dict.fromkeys(_stems(text)))[:24]
        if not terms:
            return []
        q = " OR ".join(f'"{t}"*' for t in terms)
        rows = self.store.all("SELECT task_id, bm25(cases_fts) AS score FROM cases_fts WHERE cases_fts MATCH ? "
                              "ORDER BY score LIMIT ?", (q, limit + 1))
        out = []
        for r in rows:
            if r["task_id"] == exclude:
                continue
            c = self.get(r["task_id"])
            if c:
                out.append(c)
        return out[:limit]

    def brief(self, c: dict) -> dict:
        """Что агент видит о прошлом деле: без длинных текстов, но с уроками и исходом."""
        return {"задача": c["task_id"], "запрос": (c["request"] or "")[:400], "стратегия": (c["strategy"] or "")[:600],
                "рассмотренные_альтернативы": c["alternatives"][:4],
                "итог": (c["result_summary"] or "")[:800], "уверенность": c["confidence"],
                "отзыв_владельца": c["owner_feedback"], "реальный_исход": c["outcome"],
                "уроки": [x["text"] for x in c["lessons"]], "стоимость_usd": round(c["cost_usd"] or 0, 2),
                "статус": c["status"], "результат_в_файле": c["result_path"]}

    def recent(self, limit: int = 20) -> list[dict]:
        return [self.get(r["task_id"]) for r in
                self.store.all("SELECT task_id FROM cases ORDER BY updated_at DESC LIMIT ?", (limit,))]
