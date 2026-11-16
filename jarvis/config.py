"""Конфигурация: YAML для настроек, окружение — для секретов."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class OwnerCfg(BaseModel):
    name: str = "Владелец"
    timezone: str = "UTC"
    rules: list[str] = Field(default_factory=list)
    profile: dict[str, str] = Field(default_factory=dict)


class BudgetCfg(BaseModel):
    day_usd: float = 10.0
    month_usd: float = 300.0
    task_usd: dict[str, float] = Field(default_factory=lambda: {"A": 1.0, "B": 0.3, "C": 0.02})
    est_call_usd: float = 0.02


class LimitsCfg(BaseModel):
    max_rounds: int = 2
    depth_cap: int = 3
    fanout_cap: int = 4
    clarify_max_questions: int = 3
    provider_pause_minutes: int = 30


class RouterCfg(BaseModel):
    class_a_keywords: list[str] = Field(default_factory=lambda: [
        "деньг", "перевод", "оплат", "купить", "вывод", "usdt", "крипт", "банк",
        "налог", "виз", "юрид", "договор", "инвест", "паспорт", "удали", "опубликуй",
    ])
    class_c_keywords: list[str] = Field(default_factory=lambda: ["напомни", "погода", "адрес", "доставк"])
    class_c_max_words: int = 12
    # Признаки данных, которые нельзя отправлять в облако даже обезличенно — проверяются ДО приёма задачи.
    local_only_keywords: list[str] = Field(default_factory=lambda: [
        "номер паспорта", "паспортные данные", "медицин", "анализ крови", "диагноз", "пароль", "seed", "мнемон", "приватный ключ",
    ])


class SourcesCfg(BaseModel):
    tier1_domains: list[str] = Field(default_factory=lambda: [".gov", ".go.th", ".gov.ru"])
    tier2_domains: list[str] = Field(default_factory=lambda: ["reuters.com", "wikipedia.org"])


DEFAULT_MODELS: dict[str, list[str]] = {
    "card": ["anthropic/claude-sonnet-5.5"],
    "solo": ["anthropic/claude-sonnet-5.5", "openai/gpt-6-luna"],
    "planner": ["anthropic/claude-sonnet-5.5"],
    "worker": ["~deepseek/deepseek-v4-flash-latest", "anthropic/claude-haiku-5.5"],
    "synth": ["anthropic/claude-sonnet-5.5"],
    "critic": ["openai/gpt-6-luna", "~google/gemini-flash-latest", "anthropic/claude-sonnet-5.5"],
    "reader": ["anthropic/claude-haiku-5.5", "~google/gemini-flash-latest"],
}


class Settings(BaseModel):
    owner: OwnerCfg = Field(default_factory=OwnerCfg)
    budget: BudgetCfg = Field(default_factory=BudgetCfg)
    limits: LimitsCfg = Field(default_factory=LimitsCfg)
    router: RouterCfg = Field(default_factory=RouterCfg)
    sources: SourcesCfg = Field(default_factory=SourcesCfg)
    models: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_MODELS))
    searxng_url: str | None = None
    local_llm_url: str | None = None
    openrouter_url: str = "https://openrouter.ai/api/v1"
    db_path: str = "data/jarvis.db"

    # Секреты — только из окружения, в YAML их нет.
    openrouter_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_owner_id: int | None = None

    def task_cap(self, cls: str) -> float:
        return float(self.budget.task_usd.get(cls, self.budget.task_usd.get("B", 0.3)))


def load_settings(path: str | Path | None = None, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    cfg_path = Path(path or env.get("JARVIS_CONFIG", "config/jarvis.yaml"))
    data = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    for secret in ("openrouter_api_key", "telegram_bot_token", "telegram_owner_id"):
        data.pop(secret, None)  # секреты из файла игнорируем намеренно
    settings = Settings(**data)
    settings.openrouter_api_key = env.get("OPENROUTER_API_KEY") or None
    settings.telegram_bot_token = env.get("TELEGRAM_BOT_TOKEN") or None
    owner = env.get("TELEGRAM_OWNER_ID")
    settings.telegram_owner_id = int(owner) if owner and owner.strip().lstrip("-").isdigit() else None
    if env.get("JARVIS_DB"):
        settings.db_path = env["JARVIS_DB"]
    # адреса инфраструктуры сервера (SearXNG, локальная модель через Tailscale) — из окружения
    if env.get("JARVIS_SEARXNG_URL"):
        settings.searxng_url = env["JARVIS_SEARXNG_URL"]
    if env.get("JARVIS_LOCAL_LLM_URL"):
        settings.local_llm_url = env["JARVIS_LOCAL_LLM_URL"]
    return settings
