"""Роутер сложности на правилах. Модель может поднять класс задачи, но не опустить."""
from __future__ import annotations

from .card import TaskClass
from .config import RouterCfg


REMINDER_PREFIXES = ("напомни", "напомнить", "поставь напоминание")


def classify(text: str, cfg: RouterCfg) -> TaskClass:
    t = text.lower().strip()
    # Напоминание ничего не делает с деньгами и правом само — это быт, даже если речь о деньгах.
    if t.startswith(REMINDER_PREFIXES):
        return TaskClass.C
    if any(k.lower() in t for k in cfg.class_a_keywords):
        return TaskClass.A
    if len(t.split()) <= cfg.class_c_max_words and any(k.lower() in t for k in cfg.class_c_keywords):
        return TaskClass.C
    return TaskClass.B


def needs_local(text: str, cfg: RouterCfg) -> bool:
    """Запрос с такими данными не уходит в облако, пока владелец не разрешит."""
    t = text.lower()
    return any(k.lower() in t for k in cfg.local_only_keywords)


def merge_class(rule: TaskClass, model: TaskClass) -> TaskClass:
    """Правила — нижняя граница: модель может только повысить класс."""
    return rule if rule.rank >= model.rank else model


def is_reminder(text: str) -> bool:
    return text.strip().lower().startswith(REMINDER_PREFIXES)
