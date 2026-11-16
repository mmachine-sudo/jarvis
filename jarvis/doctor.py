"""`jarvis doctor` — проверка перед запуском: секреты, id моделей, Telegram, база, поиск.

Значения секретов не печатает никогда — только «есть/нет».
"""
from __future__ import annotations

import difflib

import httpx

from .config import Settings
from .store import Store


def check(settings: Settings, client: httpx.Client | None = None) -> tuple[bool, list[str]]:
    client = client or httpx.Client(timeout=20)
    lines: list[str] = []
    ok = True

    for name, val in (("OPENROUTER_API_KEY", settings.openrouter_api_key),
                      ("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token),
                      ("TELEGRAM_OWNER_ID", settings.telegram_owner_id)):
        lines.append(f"{'✓' if val else '✗'} {name}: {'есть' if val else 'нет в окружении'}")
        ok &= bool(val)

    try:
        resp = client.get(settings.openrouter_url.rstrip("/") + "/models")
        resp.raise_for_status()
        known = {m["id"] for m in resp.json().get("data", [])}
        for role, models in settings.models.items():
            for m in models:
                if m.startswith("local/"):
                    lines.append(f"• {role}: {m} — локальная, проверь local_llm_url")
                elif m in known:
                    lines.append(f"✓ {role}: {m}")
                else:
                    ok = False
                    hint = difflib.get_close_matches(m, known, n=3, cutoff=0.6)
                    lines.append(f"✗ {role}: {m} нет на OpenRouter" + (f" — возможно: {', '.join(hint)}" if hint else ""))
    except httpx.HTTPError as e:
        ok = False
        lines.append(f"✗ OpenRouter /models недоступен: {type(e).__name__}")

    critic_vendors = {m.split('/')[0] for m in settings.models.get("critic", [])}
    if len(critic_vendors) < 2:
        ok = False
        lines.append("✗ У критика нужен минимум 2 вендора, иначе он может совпасть с автором")

    if settings.telegram_bot_token:
        try:
            r = client.get(f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe")
            good = r.status_code == 200 and r.json().get("ok")
            lines.append(f"{'✓' if good else '✗'} Telegram-бот: {'@' + r.json()['result']['username'] if good else 'токен не принят'}")
            ok &= bool(good)
        except httpx.HTTPError as e:
            ok = False
            lines.append(f"✗ Telegram недоступен: {type(e).__name__}")

    try:
        Store(settings.db_path).set("doctor_ping", 1)
        lines.append(f"✓ База: {settings.db_path}")
    except Exception as e:
        ok = False
        lines.append(f"✗ База {settings.db_path}: {e}")

    if settings.searxng_url:
        try:
            r = client.get(settings.searxng_url.rstrip("/") + "/search", params={"q": "test", "format": "json"})
            lines.append(f"{'✓' if r.status_code == 200 else '✗'} SearXNG: HTTP {r.status_code}")
        except httpx.HTTPError as e:
            lines.append(f"✗ SearXNG недоступен: {type(e).__name__} (Team будет работать без веба)")
    else:
        lines.append("• SearXNG не задан — Team работает без веб-поиска")
    return ok, lines
