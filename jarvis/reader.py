"""Карантинный читатель и поиск.

Читатель получает чужой текст (веб-страницу) и возвращает только типизированные
факты. У него нет инструментов, а его выход — данные, не инструкции: планировщик
и исполнители никогда не видят сырой страницы. Уровень источника ставит код.
"""
from __future__ import annotations

import html
import re

import httpx
from pydantic import BaseModel, Field

from .config import Settings
from .gateway import Gateway, as_messages
from .guard import is_public_url
from .memory import tier_of
from .prompts import READER_SYSTEM

MAX_BYTES = 400_000
MAX_CHARS = 12_000

_TAGS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def html_to_text(raw: str) -> str:
    raw = _TAGS.sub(" ", raw)
    raw = _ANY_TAG.sub(" ", raw)
    return _SPACES.sub(" ", html.unescape(raw)).strip()


class ExtractedFact(BaseModel):
    text: str
    quote: str = ""


class ReaderOut(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)
    instructions_detected: bool = False
    relevant: bool = True


class Evidence(BaseModel):
    url: str
    tier: int
    facts: list[ExtractedFact]
    instructions_detected: bool


class QuarantineReader:
    def __init__(self, settings: Settings, gateway: Gateway, client: httpx.AsyncClient | None = None):
        self.settings, self.gw = settings, gateway
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False,
                                                  headers={"User-Agent": "Jarvis-reader/0.1"})

    async def fetch(self, url: str) -> str:
        # Редиректы проходим вручную: каждый адрес проверяется, внутренняя сеть закрыта.
        for _ in range(4):
            if not is_public_url(url):
                raise PermissionError(f"адрес закрыт для читателя: {url}")
            resp = await self.client.get(url)
            if resp.is_redirect and resp.headers.get("location"):
                url = str(resp.url.join(resp.headers["location"]))
                continue
            resp.raise_for_status()
            return html_to_text(resp.text[:MAX_BYTES])[:MAX_CHARS]
        raise PermissionError("слишком много редиректов")

    async def read(self, url: str, question: str, *, task_id: str | None = None,
                   task_cap: float | None = None, text: str | None = None) -> Evidence:
        page = text if text is not None else await self.fetch(url)
        reply = await self.gw.chat(
            "reader",
            as_messages(READER_SYSTEM, f"ВОПРОС: {question}\n\n<<<НЕДОВЕРЕННЫЙ ТЕКСТ>>>\n{page}\n<<<КОНЕЦ>>>"),
            task_id=task_id, task_cap=task_cap, schema=ReaderOut, max_tokens=900)
        out: ReaderOut = reply.data
        return Evidence(url=url, tier=tier_of(url, self.settings.sources),
                        facts=out.facts[:8] if out.relevant else [],
                        instructions_detected=out.instructions_detected)


class Searcher:
    """Поиск через свой SearXNG (как в MMachine). Без адреса — поиск выключен."""

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=20)

    async def search(self, query: str, n: int = 3) -> list[str]:
        resp = await self.client.get(f"{self.base_url}/search", params={"q": query, "format": "json"})
        resp.raise_for_status()
        urls = [r.get("url") for r in resp.json().get("results", []) if r.get("url")]
        seen, out = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out[:n]
