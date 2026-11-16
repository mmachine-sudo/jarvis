"""Хранилище: SQLite (WAL) с чекпоинтами задач.

Задача хранит своё состояние после каждого этапа, поэтому перезапуск процесса
не оставляет её «висеть» (урок MMachine: схема навсегда застревала в песочнице).
Интерфейс узкий — при росте нагрузки меняется на Postgres без правок ядра.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL,
  cls TEXT, round INTEGER DEFAULT 0, card_json TEXT, result_json TEXT,
  issues_json TEXT, questions_json TEXT, answers_json TEXT, note TEXT,
  cap_multiplier REAL DEFAULT 1.0, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, actor TEXT,
  model TEXT, detail TEXT, cost_usd REAL DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS spend (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, month TEXT,
  task_id TEXT, model TEXT, usd REAL
);
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT, key TEXT, value TEXT,
  source TEXT, source_kind TEXT, tier INTEGER, valid_until TEXT,
  invalid_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, summary TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, question TEXT, p REAL,
  resolve_by TEXT, outcome INTEGER, asked_at TEXT, resolved_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload_json TEXT,
  status TEXT DEFAULT 'open', choice TEXT, created_at TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS playbook (
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, status TEXT, source_task TEXT,
  created_at TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, payload TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # --- низкий уровень -------------------------------------------------------
    def x(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        row = self.x(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        return [dict(r) for r in self.x(sql, params).fetchall()]

    # --- kv -------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, ensure_ascii=False)))

    # --- задачи ---------------------------------------------------------------
    def new_task_id(self) -> str:
        with self._lock:
            n = int(self.get("task_seq", 0)) + 1
            self.set("task_seq", n)
        return f"T-{n:04d}"

    def create_task(self, task_id: str, request: str, status: str = "new") -> None:
        ts = now_iso()
        self.x("INSERT INTO tasks(id,request,status,created_at,updated_at) VALUES(?,?,?,?,?)",
               (task_id, request, status, ts, ts))

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        for k, v in list(fields.items()):
            if k.endswith("_json") and not isinstance(v, (str, type(None))):
                fields[k] = json.dumps(v, ensure_ascii=False)
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def task(self, task_id: str) -> dict | None:
        return self.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def tasks(self, statuses: list[str] | None = None, limit: int = 50) -> list[dict]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            return self.all(f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY created_at, id LIMIT ?",
                            (*statuses, limit))
        return self.all("SELECT * FROM tasks ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))

    @staticmethod
    def loads(value: str | None, default: Any = None) -> Any:
        return json.loads(value) if value else default

    # --- след работы ------------------------------------------------------------
    def add_step(self, task_id: str, kind: str, actor: str, detail: str,
                 model: str | None = None, cost_usd: float = 0.0) -> None:
        self.x("INSERT INTO steps(task_id,kind,actor,model,detail,cost_usd,created_at) VALUES(?,?,?,?,?,?,?)",
               (task_id, kind, actor, model, detail[:2000], cost_usd, now_iso()))

    def steps(self, task_id: str) -> list[dict]:
        return self.all("SELECT * FROM steps WHERE task_id=? ORDER BY id", (task_id,))

    def log_event(self, kind: str, payload: Any) -> None:
        self.x("INSERT INTO events(ts,kind,payload) VALUES(?,?,?)",
               (now_iso(), kind, json.dumps(payload, ensure_ascii=False)))

    def events(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        if kind:
            return self.all("SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
        return self.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # --- согласования -------------------------------------------------------------
    def add_approval(self, task_id: str | None, kind: str, payload: dict) -> int:
        cur = self.x("INSERT INTO approvals(task_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                     (task_id, kind, json.dumps(payload, ensure_ascii=False), now_iso()))
        return int(cur.lastrowid)

    def approval(self, approval_id: int) -> dict | None:
        return self.one("SELECT * FROM approvals WHERE id=?", (approval_id,))

    def open_approvals(self) -> list[dict]:
        return self.all("SELECT * FROM approvals WHERE status='open' ORDER BY id")

    def close_approval(self, approval_id: int, choice: str) -> None:
        self.x("UPDATE approvals SET status='closed', choice=?, decided_at=? WHERE id=?",
               (choice, now_iso(), approval_id))
