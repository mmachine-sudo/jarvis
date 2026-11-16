"""Шлюз моделей: OpenRouter (+ локальная модель), лимиты, откаты по ролям.

Зашитые уроки MMachine:
  * 402 от OpenRouter → пауза всех вызовов (баланс), а не бесконечные повторы;
  * пустой ответ reasoning-модели → один повтор с лимитом токенов ×4;
  * сломанный JSON → следующий кандидат, а не падение задачи;
  * каждый вызов проходит предпроверку бюджета и записывается в казну.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .store import Store
from .treasury import Treasury


class ModelError(Exception):
    pass


class ProviderPaused(ModelError):
    pass


class AllCandidatesFailed(ModelError):
    pass


@dataclass
class Reply:
    text: str
    data: Any
    model: str
    vendor: str
    usd: float


def vendor_of(model_id: str) -> str:
    return model_id.split("/", 1)[0].lstrip("~")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> str:
    """Достаёт JSON-объект из ответа: убирает ```-обёртки и текст вокруг."""
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("в ответе нет JSON-объекта")
    return text[start:end + 1]


class Gateway(Protocol):
    async def chat(self, role: str, messages: list[dict], *, task_id: str | None = None,
                   max_tokens: int = 1500, schema: type[BaseModel] | None = None,
                   exclude_vendors: tuple[str, ...] = (), task_cap: float | None = None,
                   temperature: float = 0.2) -> Reply: ...


class ModelGateway:
    PAUSE_KEY = "provider_paused_until"

    def __init__(self, settings: Settings, treasury: Treasury, store: Store,
                 transport: httpx.AsyncBaseTransport | None = None, clock=time.time):
        self.settings, self.treasury, self.store = settings, treasury, store
        self.client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(120, connect=15))
        self.clock = clock

    async def aclose(self) -> None:
        await self.client.aclose()

    # --- пауза провайдера --------------------------------------------------------
    def paused_until(self) -> float:
        return float(self.store.get(self.PAUSE_KEY, 0) or 0)

    def is_paused(self) -> bool:
        return self.paused_until() > self.clock()

    def _pause(self) -> None:
        until = self.clock() + self.settings.limits.provider_pause_minutes * 60
        self.store.set(self.PAUSE_KEY, until)
        self.store.log_event("provider_paused", {"until": until, "reason": "402"})

    def resume_provider(self) -> None:
        self.store.set(self.PAUSE_KEY, 0)

    # --- вызов ------------------------------------------------------------------------
    def candidates(self, role: str, exclude_vendors: tuple[str, ...] = ()) -> list[str]:
        models = self.settings.models.get(role) or []
        return [m for m in models if vendor_of(m) not in exclude_vendors]

    async def chat(self, role: str, messages: list[dict], *, task_id: str | None = None,
                   max_tokens: int = 1500, schema: type[BaseModel] | None = None,
                   exclude_vendors: tuple[str, ...] = (), task_cap: float | None = None,
                   temperature: float = 0.2) -> Reply:
        cands = self.candidates(role, exclude_vendors)
        if not cands:
            raise AllCandidatesFailed(f"нет моделей для роли «{role}» (исключены: {', '.join(exclude_vendors) or '—'})")
        errors: list[str] = []
        for model in cands:
            local = model.startswith("local/")
            if not local and self.is_paused():
                raise ProviderPaused("OpenRouter на паузе: закончился баланс (402)")
            tokens = max_tokens
            for attempt in range(2):
                self.treasury.check(task_id, 0.0 if local else self.settings.budget.est_call_usd, task_cap)
                try:
                    resp = await self._post(model, messages, tokens, schema is not None, temperature)
                except httpx.HTTPError as e:
                    errors.append(f"{model}: сеть ({type(e).__name__})")
                    break
                if resp.status_code == 402 and not local:
                    self._pause()
                    raise ProviderPaused("OpenRouter ответил 402: нужен пополненный баланс")
                if resp.status_code >= 400:
                    errors.append(f"{model}: HTTP {resp.status_code}")
                    break
                try:
                    body = resp.json()
                except ValueError:
                    errors.append(f"{model}: ответ не JSON")
                    break
                usd = 0.0 if local else self._cost(body)
                self.treasury.record(task_id, model, usd)
                text = self._content(body)
                if not text.strip():
                    if attempt == 0:
                        tokens *= 4  # урок MMachine: пустой ответ reasoning-модели
                        continue
                    errors.append(f"{model}: пустой ответ")
                    break
                if schema is None:
                    return Reply(text, None, model, vendor_of(model), usd)
                try:
                    data = schema.model_validate_json(extract_json(text))
                except (ValueError, ValidationError) as e:
                    errors.append(f"{model}: JSON не по схеме ({str(e).splitlines()[0][:120]})")
                    break  # урок MMachine: сломанный JSON → следующий кандидат
                return Reply(text, data, model, vendor_of(model), usd)
        raise AllCandidatesFailed("; ".join(errors) or "все кандидаты отказали")

    async def _post(self, model: str, messages: list[dict], max_tokens: int,
                    want_json: bool, temperature: float) -> httpx.Response:
        if model.startswith("local/"):
            if not self.settings.local_llm_url:
                raise httpx.ConnectError("local_llm_url не задан")
            url = self.settings.local_llm_url.rstrip("/") + "/chat/completions"
            headers: dict[str, str] = {}
            name = model.split("/", 1)[1]
        else:
            url = self.settings.openrouter_url.rstrip("/") + "/chat/completions"
            headers = {"Authorization": f"Bearer {self.settings.openrouter_api_key or ''}", "X-Title": "Jarvis"}
            name = model
        payload: dict[str, Any] = {"model": name, "messages": messages, "max_tokens": max_tokens,
                                   "temperature": temperature}
        if not model.startswith("local/"):
            payload["usage"] = {"include": True}
        if want_json:
            payload["response_format"] = {"type": "json_object"}
        return await self.client.post(url, json=payload, headers=headers)

    def _cost(self, body: dict) -> float:
        usage = body.get("usage") or {}
        cost = usage.get("cost")
        if isinstance(cost, (int, float)):
            return float(cost)
        return self.settings.budget.est_call_usd  # консервативно, если цена не пришла

    @staticmethod
    def _content(body: dict) -> str:
        try:
            content = body["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            return ""
        if isinstance(content, list):  # content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content or ""


class LocalOnlyGateway:
    """Для приватных задач: любая роль идёт только в локальную модель (роль «local»).

    Критик в этом режиме — та же локальная модель: независимость ниже, но данные
    не покидают машину. Это осознанный компромисс фазы 1.
    """

    def __init__(self, inner: Gateway):
        self.inner = inner

    async def chat(self, role: str, messages: list[dict], **kw: Any) -> Reply:
        kw["exclude_vendors"] = ()
        return await self.inner.chat("local", messages, **kw)


def as_messages(system: str, user: str | dict) -> list[dict]:
    if not isinstance(user, str):
        user = json.dumps(user, ensure_ascii=False, indent=1)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
