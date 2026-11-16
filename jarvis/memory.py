"""Память: факты с источником, уровнем доверия и сроком; эпизоды; плейбук.

Правила шлюза записи:
  * новый факт заменяет старый, только если его источник не слабее (tier ≤ старого);
  * более слабый источник создаёт конфликт → решает владелец;
  * старый факт не удаляется, а получает invalid_at (история сохраняется);
  * веб-контент пишется только в эпизоды, в плейбук — никогда.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from .config import SourcesCfg
from .store import Store, now_iso

_WORD = re.compile(r"[\wё]{3,}", re.I)


def _stem(word: str) -> str:
    """Грубая основа слова для русского: «визы»/«виза» → «виз», «Таиланде»/«Таиланд» → «таила»."""
    w = word.lower()
    return w[:5] if len(w) >= 5 else w[:max(3, len(w) - 1)]


def tier_of(url: str | None, sources: SourcesCfg) -> int:
    """Уровень источника по домену: 1 — официальный, 2 — надёжный, 3 — прочее."""
    if not url:
        return 3
    host = (urlparse(url).hostname or "").lower()

    def match(domain: str) -> bool:
        d = domain.lower().lstrip(".")
        return host == d or host.endswith("." + d)

    if any(match(d) for d in sources.tier1_domains):
        return 1
    if any(match(d) for d in sources.tier2_domains):
        return 2
    return 3


class Memory:
    def __init__(self, store: Store):
        self.store = store

    # --- факты --------------------------------------------------------------------
    def active_fact(self, namespace: str, key: str) -> dict | None:
        return self.store.one(
            "SELECT * FROM facts WHERE namespace=? AND key=? AND invalid_at IS NULL ORDER BY id DESC LIMIT 1",
            (namespace, key))

    def write_fact(self, namespace: str, key: str, value: str, *, source: str, tier: int,
                   source_kind: str = "owner", valid_until: str | None = None) -> str:
        """Возвращает 'created' | 'same' | 'replaced' | 'conflict'."""
        old = self.active_fact(namespace, key)
        if old and old["value"] == value:
            return "same"
        if old and tier > (old["tier"] or 3):
            self.store.add_approval(None, "fact_conflict", {
                "namespace": namespace, "key": key, "old": old["value"], "old_source": old["source"],
                "old_tier": old["tier"], "new": value, "new_source": source, "new_tier": tier,
                "new_kind": source_kind, "valid_until": valid_until})
            return "conflict"
        ts = now_iso()
        if old:
            self.store.x("UPDATE facts SET invalid_at=? WHERE id=?", (ts, old["id"]))
        self.store.x(
            "INSERT INTO facts(namespace,key,value,source,source_kind,tier,valid_until,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (namespace, key, value, source, source_kind, tier, valid_until, ts))
        self.store.log_event("fact", {"key": f"{namespace}.{key}", "action": "replaced" if old else "created"})
        return "replaced" if old else "created"

    def resolve_conflict(self, payload: dict, accept_new: bool) -> None:
        if not accept_new:
            return
        old = self.active_fact(payload["namespace"], payload["key"])
        ts = now_iso()
        if old:
            self.store.x("UPDATE facts SET invalid_at=? WHERE id=?", (ts, old["id"]))
        self.store.x(
            "INSERT INTO facts(namespace,key,value,source,source_kind,tier,valid_until,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (payload["namespace"], payload["key"], payload["new"], payload["new_source"] + " (подтвердил владелец)",
             "owner", 1, payload.get("valid_until"), ts))

    def facts(self, query: str | None = None, include_expired: bool = False, limit: int = 30) -> list[dict]:
        rows = self.store.all("SELECT * FROM facts WHERE invalid_at IS NULL ORDER BY id DESC")
        now = datetime.now(timezone.utc).date().isoformat()
        out = []
        for r in rows:
            r["expired"] = bool(r["valid_until"] and r["valid_until"] < now)
            if r["expired"] and not include_expired:
                continue
            if query and query.lower() not in f"{r['namespace']} {r['key']} {r['value']}".lower():
                continue
            out.append(r)
        return out[:limit]

    def seed_profile(self, profile: dict[str, str]) -> None:
        for k, v in profile.items():
            self.write_fact("profile", k, str(v), source="конфиг владельца", tier=1)

    def briefing(self, text: str, limit: int = 12) -> list[dict]:
        """Факты, относящиеся к задаче: профиль всегда, остальное — по пересечению слов."""
        words = {_stem(w) for w in _WORD.findall(text)}
        scored = []
        for f in self.facts(limit=500):
            hay = {_stem(w) for w in _WORD.findall(f"{f['key']} {f['value']}")}
            score = len(words & hay) + (0.5 if f["namespace"] == "profile" else 0)
            if score > 0:
                scored.append((score, f))
        scored.sort(key=lambda x: -x[0])
        return [{"факт": f"{f['namespace']}.{f['key']}", "значение": f["value"],
                 "источник": f["source"], "уровень": f["tier"], "действует_до": f["valid_until"]}
                for _, f in scored[:limit]]

    # --- эпизоды и плейбук --------------------------------------------------------------
    def add_episode(self, task_id: str, summary: str) -> None:
        self.store.x("INSERT INTO episodes(task_id,summary,created_at) VALUES(?,?,?)",
                     (task_id, summary[:2000], now_iso()))

    def propose_lesson(self, text: str, source_task: str) -> bool:
        """Кандидат в плейбук. В работу — только через ворота владельца. True — если кандидат новый."""
        if self.store.one("SELECT id FROM playbook WHERE text=?", (text[:500],)):
            return False
        self.store.x("INSERT INTO playbook(text,status,source_task,created_at) VALUES(?,?,?,?)",
                     (text[:500], "candidate", source_task, now_iso()))
        return True

    def lessons(self, status: str = "active") -> list[dict]:
        return self.store.all("SELECT * FROM playbook WHERE status=? ORDER BY id", (status,))

    def set_lesson_status(self, lesson_id: int, status: str) -> bool:
        cur = self.store.x("UPDATE playbook SET status=?, decided_at=? WHERE id=?", (status, now_iso(), lesson_id))
        return cur.rowcount > 0
