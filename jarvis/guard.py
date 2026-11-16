"""Страж: стоп-кнопка, уровни автономии, обезличивание и шлюз действий.

Jarvis в фазе 1 не совершает внешних действий сам: деньги, публикации и письма
превращаются в согласование с кнопками, а делает их владелец.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

from .store import Store

_REDACT = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b"), "[tron-адрес]"),
    (re.compile(r"\b(?:EQ|UQ)[A-Za-z0-9_-]{46}\b"), "[ton-адрес]"),
    (re.compile(r"\b0x[a-fA-F0-9]{40}\b"), "[evm-адрес]"),
    (re.compile(r"\b(?:\d{4}[ -]){3}\d{4}\b"), "[номер карты]"),
    (re.compile(r"\b\d{10,19}\b"), "[номер]"),
    (re.compile(r"\+?\d[\d ()-]{8,}\d"), "[телефон]"),
]

_SECRETS = [
    re.compile(r"\bsk-(?:or-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{30,40}(?![A-Za-z0-9_-])"),       # токен Telegram-бота
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def scrub_secrets(text: str) -> str:
    for p in _SECRETS:
        text = p.sub("[секрет]", text)
    return text


class SecretLogFilter(logging.Filter):
    """Вычищает токены и ключи из всех записей журнала (в т.ч. из трассировок)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub_secrets(str(record.getMessage()))
        record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = scrub_secrets(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install_log_redaction() -> None:
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretLogFilter())


def is_public_url(url: str) -> bool:
    """Только http(s) и только публичные адреса: читатель не должен ходить во внутреннюю сеть VPS."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def redact(text: str) -> str:
    """Убирает адреса, номера и контакты перед отправкой в облачные модели."""
    for pattern, repl in _REDACT:
        text = pattern.sub(repl, text)
    return text


def leaks_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRETS)


class Guard:
    STOP_KEY = "stopped"

    def __init__(self, store: Store):
        self.store = store

    def stopped(self) -> bool:
        return bool(self.store.get(self.STOP_KEY, False))

    def stop(self, reason: str = "владелец") -> None:
        self.store.set(self.STOP_KEY, True)
        self.store.log_event("stop", {"reason": reason})

    def resume(self) -> None:
        self.store.set(self.STOP_KEY, False)
        self.store.log_event("resume", {})
