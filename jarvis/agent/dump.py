"""Полная выгрузка агентной задачи для разбора: шаги и цены, дело (стратегия, уроки), файлы рабочей папки
и расшифровка сессии агента — каждый вызов инструмента с аргументами и началом ответа (поиск, страницы,
перекрёстная проверка, подагенты). Публикация — отдельной страницей Notion (бесплатно)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from ..store import Store

TEXT_EXT = {".md", ".txt", ".json", ".py", ".csv", ".yaml", ".yml", ".html"}


def _blocks_text(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "tool_result":
                out.append(_blocks_text(b.get("content")))
    return "\n".join(out)


def transcript(home: Path, workdir: Path, result_chars: int = 2500) -> list[str]:
    """Расшифровка всех сессий CLI, запущенных в рабочей папке задачи (включая подагентов)."""
    key = str(workdir).replace("/", "-").replace(".", "-")
    root = home / ".claude" / "projects"
    files = sorted({*root.glob(f"{key}/**/*.jsonl"), *root.glob(f"{key}*.jsonl")}, key=lambda p: p.stat().st_mtime)
    lines: list[str] = []
    for f in files:
        lines.append(f"\n===== сессия {f.relative_to(root)} =====")
        names: dict[str, str] = {}
        for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            msg = d.get("message") or {}
            ts = (d.get("timestamp") or "")[11:19]
            side = "  [подагент]" if d.get("isSidechain") else ""
            content = msg.get("content")
            if isinstance(content, str) and d.get("type") == "user":
                lines.append(f"{ts}{side} ЗАПРОС: {content[:3000]}")
                continue
            for b in content or []:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text" and d.get("type") == "assistant" and b.get("text", "").strip():
                    lines.append(f"{ts}{side} АГЕНТ: {b['text'][:2500]}")
                elif t == "tool_use":
                    names[b.get("id", "")] = b.get("name", "")
                    lines.append(f"{ts}{side} ВЫЗОВ {b.get('name')}: {json.dumps(b.get('input'), ensure_ascii=False)[:1500]}")
                elif t == "tool_result":
                    name = names.get(b.get("tool_use_id", ""), "?")
                    lim = 6000 if name.endswith(("cross_check", "web_search", "recall_cases")) else result_chars
                    txt = _blocks_text(b.get("content")).strip()
                    lines.append(f"{ts}{side}   ← {name}{' (ошибка)' if b.get('is_error') else ''}: "
                                 f"{txt[:lim]}{' …[обрезано, всего ' + str(len(txt)) + ' симв.]' if len(txt) > lim else ''}")
            usage = msg.get("usage")
            if usage and d.get("type") == "assistant" and msg.get("model"):
                pass
    return lines


def dump(store: Store, tid: str, workroot: str) -> str:
    t = store.task(tid)
    if not t:
        return f"# {tid}: нет такой задачи"
    out = [f"# Разбор {tid} · {t['status']} · класс {t['cls']}", f"Запрос: {t['request']}", ""]
    payload = Store.loads(t["result_json"], {}) or {}
    out.append("## Служебное: " + json.dumps({k: v for k, v in payload.items() if k not in ("result", "next_prompt")},
                                             ensure_ascii=False)[:1500])
    try:
        c = store.conn.execute("SELECT * FROM cases WHERE task_id=?", (tid,)).fetchone()
        if c:
            out.append("## Дело\n" + json.dumps(dict(c), ensure_ascii=False, indent=1)[:12000])
    except Exception as e:  # noqa: BLE001
        out.append(f"(дело не прочитано: {e})")
    out.append("## Шаги")
    total = 0.0
    for s in store.steps(tid):
        total += s["cost_usd"] or 0
        out.append(f"{s['created_at'][11:19]} {s['kind']}/{s['actor']} {s['model'] or ''} ${s['cost_usd'] or 0:.3f} · "
                   f"{(s['detail'] or '')[:1200]}")
    out.append(f"Итого по шагам: ${total:.3f}")
    wd = Path(workroot) / tid
    out.append(f"## Файлы {wd}")
    if wd.is_dir():
        for p in sorted(wd.rglob("*")):
            if p.is_file():
                rel = p.relative_to(wd)
                out.append(f"\n----- {rel} ({p.stat().st_size} байт) -----")
                if p.suffix.lower() in TEXT_EXT and p.stat().st_size < 120_000:
                    out.append(p.read_text(encoding="utf-8", errors="replace"))
    out.append("## Расшифровка сессии агента")
    out += transcript(Path(workroot) / ".home", wd)
    return "\n".join(out)


def publish_notion(token: str, parent: str, title: str, text: str) -> str:
    """Страница-дочка с текстом блоками по 1900 символов (ограничение Notion на один кусок текста)."""
    h = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    chunks, buf = [], ""
    for line in text.splitlines():
        while len(line) > 1900:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:1900])
            line = line[1900:]
        if len(buf) + len(line) + 1 > 1900:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    if buf:
        chunks.append(buf)
    blocks = [{"object": "block", "type": "code", "code": {"language": "plain text",
               "rich_text": [{"type": "text", "text": {"content": c}}]}} for c in chunks]
    with httpx.Client(timeout=60, headers=h) as cl:
        r = cl.post("https://api.notion.com/v1/pages", json={
            "parent": {"page_id": parent}, "properties": {"title": {"title": [{"text": {"content": title}}]}},
            "children": blocks[:90]})
        r.raise_for_status()
        pid = r.json()["id"]
        for i in range(90, len(blocks), 90):
            cl.patch(f"https://api.notion.com/v1/blocks/{pid}/children", json={"children": blocks[i:i + 90]}).raise_for_status()
    return pid
