"""Оркестратор: жизненный цикл задачи с чекпоинтами после каждого этапа.

new → (awaiting_clarification) → ready → executing → verifying → done
                                              ↑            │
                                              └── rework ←─┘ (≤ max_rounds, затем awaiting_owner)
Класс A и любые действия владельца всегда заканчиваются awaiting_owner.
"""
from __future__ import annotations

import json
from typing import Protocol

from .card import Privacy, Result, TaskCard, TaskClass
from .config import Settings
from .gateway import AllCandidatesFailed, Gateway, LocalOnlyGateway, ProviderPaused, Reply
from .guard import Guard
from .intake import draft_card
from .rework import Rework, place_of
from .router import needs_local
from .journal import Journal
from .memory import Memory
from .modes import ExecContext, run_solo, run_team
from .reader import QuarantineReader, Searcher
from .store import Store
from .treasury import BudgetExceeded
from .verify import accepted, checklist, consilium_review, critic_review, official_source_gap

ACTIVE = ["new", "ready", "executing", "verifying", "rework"]
TERMINAL = {"done", "cancelled", "failed"}


class Notifier(Protocol):
    async def notify(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None: ...


def buttons_for(approval_id: int, options: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(label, f"ap:{approval_id}:{choice}") for label, choice in options]


class Orchestrator:
    def __init__(self, settings: Settings, store: Store, gw: Gateway, memory: Memory, journal: Journal,
                 guard: Guard, notifier: Notifier, reader: QuarantineReader | None = None,
                 searcher: Searcher | None = None):
        self.s, self.store, self.gw = settings, store, gw
        self.memory, self.journal, self.guard, self.notifier = memory, journal, guard, notifier
        self.reader, self.searcher = reader, searcher
        self.rework = Rework(store)

    # --- вход ----------------------------------------------------------------------
    def submit(self, request: str) -> str:
        tid = self.store.new_task_id()
        self.store.create_task(tid, request)
        self.store.add_step(tid, "received", "owner", request[:300])
        return tid

    def _log(self, tid: str):
        def log(kind: str, actor: str, detail: str, reply: Reply | None) -> None:
            self.store.add_step(tid, kind, actor, detail, reply.model if reply else None,
                                reply.usd if reply else 0.0)
        return log

    def card_of(self, task: dict) -> TaskCard | None:
        raw = task.get("card_json")
        return TaskCard.model_validate_json(raw) if raw else None

    # --- цикл ----------------------------------------------------------------------------
    async def run_pending(self) -> int:
        """Продвигает все активные задачи на шаг вперёд. Возвращает число продвинутых."""
        if self.guard.stopped():
            return 0
        advanced = 0
        for t in self.store.tasks(statuses=ACTIVE, limit=20):
            if self.guard.stopped():
                break
            ok = await self.step(t["id"])
            if ok is None:
                break
            advanced += ok
        return advanced

    async def step(self, tid: str) -> bool | None:
        """Один шаг задачи с обработкой сбоев. True — продвинута, False — сбой обработан, None — пауза провайдера."""
        try:
            await self.advance(tid)
            return True
        except BudgetExceeded as e:
            await self._budget_exceeded(tid, e)
        except ProviderPaused as e:
            self.store.add_step(tid, "paused", "gateway", str(e))
            await self.notifier.notify(f"⏸ {e}. Задачи ждут; после пополнения напиши /resume_models.")
            return None
        except AllCandidatesFailed as e:
            await self._models_failed(tid, e)
        return False

    async def advance(self, tid: str) -> None:
        t = self.store.task(tid)
        status = t["status"]
        if status == "new":
            await self._intake(t)
        elif status in ("ready", "executing", "rework"):
            await self._execute(t)
        elif status == "verifying":
            await self._verify(t)

    # --- этапы ---------------------------------------------------------------------------------
    def _gw_for(self, card: TaskCard | None) -> Gateway:
        if card is not None and card.privacy == Privacy.LOCAL_ONLY and self._has_local_model():
            return LocalOnlyGateway(self.gw)
        return self.gw

    async def _intake(self, t: dict) -> None:
        tid = t["id"]
        answers = Store.loads(t["answers_json"], [])
        allow_q = not answers and not Store.loads(t["questions_json"], None)
        cloud_ok = self.store.get(f"privacy_override:{tid}") == "cloud"
        sensitive = needs_local(t["request"] + " " + " ".join(answers), self.s.router) and not cloud_ok
        gw = self.gw
        if sensitive:
            if not self._has_local_model():
                # Ничего не отправляем в облако, пока владелец не разрешит.
                aid = self.store.add_approval(tid, "privacy", {"stage": "intake"})
                self.store.update_task(tid, status="awaiting_owner", note="приватные данные, нужна локальная модель")
                await self.notifier.notify(
                    f"🔒 {tid}: в запросе приватные данные, а локальная модель не подключена. "
                    f"Пока ничего не отправлял в облако.\nМожно выполнить в облаке с обезличиванием или отменить.",
                    buttons_for(aid, [("В облаке, обезличенно", "cloud"), ("Отменить", "cancel")]))
                return
            gw = LocalOnlyGateway(self.gw)
        card, questions, usd, model = await draft_card(tid, t["request"], answers, gw=gw,
                                                       memory=self.memory, settings=self.s,
                                                       allow_questions=allow_q)
        if card is not None:
            if sensitive:
                card.privacy = Privacy.LOCAL_ONLY
            elif cloud_ok and card.privacy == Privacy.LOCAL_ONLY:
                card.privacy = Privacy.REDACTED_CLOUD
        self.store.add_step(tid, "intake", "jarvis", "уточняющие вопросы" if questions else "карточка готова",
                            model, usd)
        if questions:
            self.store.update_task(tid, status="awaiting_clarification", questions_json=questions)
            q = "\n".join(f"{i}. {x}" for i, x in enumerate(questions, 1))
            await self.notifier.notify(f"❓ {tid}: уточни, пожалуйста —\n{q}\n\nОтветь одним сообщением "
                                       f"(или /answer {tid} текст).")
            return
        self.store.update_task(tid, status="ready", cls=card.cls.value, card_json=card.model_dump_json())
        if card.privacy == Privacy.LOCAL_ONLY and not self._has_local_model():
            aid = self.store.add_approval(tid, "privacy", {})
            self.store.update_task(tid, status="awaiting_owner", note="нужна локальная модель")
            await self.notifier.notify(
                f"🔒 {tid}: задача помечена «только локально», а локальная модель не подключена.\n"
                f"Можно выполнить в облаке с обезличиванием или отменить.",
                buttons_for(aid, [("В облаке, обезличенно", "cloud"), ("Отменить", "cancel")]))

    def _has_local_model(self) -> bool:
        return bool(self.s.local_llm_url) and any(m.startswith("local/") for m in self.s.models.get("local", []))

    async def _execute(self, t: dict) -> None:
        tid = t["id"]
        card = self.card_of(t)
        mult = float(t.get("cap_multiplier") or 1.0)
        card.budget.usd_cap *= mult
        self.store.update_task(tid, status="executing")  # чекпоинт
        ctx = ExecContext(card=card, briefing=self.memory.briefing(card.goal + " " + t["request"]),
                          rework_notes=Store.loads(t["issues_json"], []),
                          lessons=[x["text"] for x in self.memory.lessons("active")],
                          settings=self.s, gw=self._gw_for(card), log=self._log(tid),
                          reader=self.reader, searcher=self.searcher)
        result = await (run_solo(ctx) if card.mode.value == "solo" else run_team(ctx))
        self.store.update_task(tid, status="verifying", result_json={"result": result.model_dump(),
                                                                     "author": ctx.author,
                                                                     "security": ctx.security_flags})
        await self._verify(self.store.task(tid))

    async def _verify(self, t: dict) -> None:
        tid = t["id"]
        card = self.card_of(t)
        card.budget.usd_cap *= float(t.get("cap_multiplier") or 1.0)
        payload = Store.loads(t["result_json"], {})
        result = Result.model_validate(payload["result"])
        problems = checklist(card, result)
        if card.cls == TaskClass.A:  # консилиум: до трёх вендоров, большинство и ни одного major/critical
            verdict, replies = await consilium_review(card, result, payload.get("author", ""),
                                                      gw=self._gw_for(card), settings=self.s)
            for r in replies:
                self.store.add_step(tid, "verify", "consilium", f"голос {r.model}", r.model, r.usd)
            self.store.add_step(tid, "verify", "consilium",
                                f"чек-лист: {len(problems)} нарушений; консилиум ({len(replies)}): "
                                f"{'принято' if verdict.passed else 'не принято'}, худшее — {verdict.severity}")
        else:
            verdict, reply = await critic_review(card, result, payload.get("author", ""), gw=self._gw_for(card),
                                                 settings=self.s)
            self.store.add_step(tid, "verify", "critic",
                                f"чек-лист: {len(problems)} нарушений; критик: {verdict.severity}", reply.model, reply.usd)
        if accepted(problems, verdict):
            notes = list(verdict.issues)
            if official_source_gap(card, result):
                notes.insert(0, f"⚠️ Нет официального источника (уровень {card.evidence.min_tier}): "
                                "перед решением сверь ключевые пункты на сайте ведомства")
            await self._finish(tid, card, result, notes, payload.get("security", []))
            return
        issues = problems + verdict.issues
        rnd = int(t["round"] or 0) + 1
        # учёт потерь на доработке; причины раз в сутки сводит Rework.daily_review в кандидаты плейбука
        self.rework.log_round(tid, place_of(card.mode.value, card.cls.value), rnd, issues)
        if rnd > card.budget.max_rounds:
            aid = self.store.add_approval(tid, "escalation", {"issues": issues})
            self.store.update_task(tid, status="awaiting_owner", round=rnd, issues_json=issues,
                                   note="не прошло проверку после доработок")
            await self.notifier.notify(
                f"⚠️ {tid}: после {card.budget.max_rounds} кругов доработки проверка не пройдена.\n"
                f"Замечания:\n" + "\n".join(f"• {x}" for x in issues[:6]) +
                f"\n\nЛучший вариант ответа:\n{result.answer[:1500]}",
                buttons_for(aid, [("Принять как есть", "accept"), ("Ещё 2 круга", "rework"), ("Отменить", "cancel")]))
            return
        self.store.update_task(tid, status="rework", round=rnd, issues_json=issues)
        self.store.add_step(tid, "rework", "jarvis", f"круг {rnd}: " + "; ".join(issues[:3]))

    async def _finish(self, tid: str, card: TaskCard, result: Result, minor: list[str], security: list[str]) -> None:
        text = self._render(tid, card, result, minor, security)
        if card.cls == TaskClass.A:  # B и C — обратимое: результат сразу, «что делаешь ты» — списком в ответе
            aid = self.store.add_approval(tid, "decision", {})
            self.store.update_task(tid, status="awaiting_owner", note="ждёт решения владельца")
            await self.notifier.notify(text, buttons_for(aid, [("Принять", "accept"), ("Доработать", "rework"),
                                                               ("Отклонить", "reject")]))
            return
        self._complete(tid, card, result)
        await self.notifier.notify(text)

    def _complete(self, tid: str, card: TaskCard, result: Result) -> None:
        self.store.update_task(tid, status="done")
        self.memory.add_episode(tid, f"{card.goal} → {result.answer[:600]}")
        if card.forecast_question and result.forecast_p is not None:
            self.journal.add(tid, card.forecast_question, result.forecast_p)
        self.store.add_step(tid, "done", "jarvis", "задача закрыта")

    def _render(self, tid: str, card: TaskCard, result: Result, minor: list[str], security: list[str]) -> str:
        spent = sum(s["cost_usd"] or 0 for s in self.store.steps(tid))
        parts = [f"✅ {tid} · класс {card.cls.value} · ${spent:.2f}", result.answer]
        if result.owner_actions:
            parts.append("Что делаешь ты:\n" + "\n".join(f"• {a}" for a in result.owner_actions))
        srcs = [c for c in result.claims if c.source_url][:5]
        if srcs:
            parts.append("Источники:\n" + "\n".join(f"• [{c.tier}] {c.source_url}" for c in srcs))
        if card.forecast_question and result.forecast_p is not None:
            parts.append(f"Прогноз в журнал: «{card.forecast_question}» — p = {result.forecast_p:.2f}")
            adj = self.journal.adjust(result.forecast_p)
            if adj["active"] and abs(adj["p"] - result.forecast_p) >= 0.05:
                parts.append(f"С поправкой на историю прогнозов ({adj['bin_n']} похожих, сбылось "
                             f"{adj['bin_hit_rate']:.0%}): p ≈ {adj['p']:.2f}")
        if minor:
            parts.append("Мелкие замечания критика:\n" + "\n".join(f"• {x}" for x in minor[:3]))
        if security:
            parts.append("Безопасность:\n" + "\n".join(f"• {x}" for x in security[:3]))
        return "\n\n".join(parts)

    # --- ответы владельца -----------------------------------------------------------------------------
    def answer_clarification(self, tid: str, text: str) -> bool:
        t = self.store.task(tid)
        if not t or t["status"] != "awaiting_clarification":
            return False
        answers = Store.loads(t["answers_json"], []) + [text]
        self.store.update_task(tid, status="new", answers_json=answers)
        self.store.add_step(tid, "answer", "owner", text[:300])
        return True

    async def decide(self, approval_id: int, choice: str) -> str:
        ap = self.store.approval(approval_id)
        if not ap or ap["status"] != "open":
            return "Это решение уже принято или не найдено."
        self.store.close_approval(approval_id, choice)
        kind, tid = ap["kind"], ap["task_id"]
        payload = json.loads(ap["payload_json"] or "{}")
        if kind == "fact_conflict":
            self.memory.resolve_conflict(payload, accept_new=(choice == "new"))
            return "Факт обновлён." if choice == "new" else "Оставил прежний факт."
        t = self.store.task(tid) if tid else None
        if not t:
            return "Задача не найдена."
        card = self.card_of(t)
        self.store.add_step(tid, "decision", "owner", f"{kind}: {choice}")
        if choice in ("cancel", "reject"):
            self.store.update_task(tid, status="cancelled")
            return f"{tid} отменена."
        if kind == "privacy" and choice == "cloud" and card is None:  # решение до приёма задачи
            self.store.set(f"privacy_override:{tid}", "cloud")
            self.store.update_task(tid, status="new")
            return f"{tid}: выполняю в облаке с обезличиванием."
        if kind == "privacy" and choice == "cloud":
            self.store.set(f"privacy_override:{tid}", "cloud")
            card.privacy = Privacy.REDACTED_CLOUD
            self.store.update_task(tid, status="ready", card_json=card.model_dump_json())
            return f"{tid}: выполняю в облаке с обезличиванием."
        if kind == "budget" and choice == "extend":
            mult = float(t.get("cap_multiplier") or 1.0) * 2
            self.store.update_task(tid, status=payload.get("resume_status", "ready"), cap_multiplier=mult)
            return f"{tid}: лимит задачи увеличен вдвое, продолжаю."
        if choice == "rework" and card is None:  # сбой ещё на приёме задачи — начинаем заново
            self.store.update_task(tid, status="new")
            return f"{tid}: пробую ещё раз."
        if choice == "rework":
            issues = Store.loads(t["issues_json"], []) or ["Владелец попросил доработать"]
            self.store.update_task(tid, status="rework", round=0, issues_json=issues)
            return f"{tid}: отправил на доработку."
        if choice == "accept":
            payload_r = Store.loads(t["result_json"], {})
            if card is None or "result" not in payload_r:
                self.store.update_task(tid, status="cancelled")
                return f"{tid}: принимать нечего — результата нет, задача закрыта."
            result = Result.model_validate(payload_r["result"])
            self._complete(tid, card, result)
            return f"{tid}: принято и закрыто."
        return "Неизвестный вариант."

    # --- сбои ------------------------------------------------------------------------------
    async def _budget_exceeded(self, tid: str, e: BudgetExceeded) -> None:
        t = self.store.task(tid)
        self.store.add_step(tid, "budget", "treasury", str(e))
        if e.scope.startswith("задача"):
            aid = self.store.add_approval(tid, "budget", {"resume_status": t["status"]})
            self.store.update_task(tid, status="awaiting_owner", note=str(e))
            await self.notifier.notify(f"💸 {tid}: {e}.", buttons_for(aid, [("Удвоить лимит задачи", "extend"),
                                                                           ("Остановить задачу", "cancel")]))
        else:
            self.guard.stop(f"бюджет: {e.scope}")
            await self.notifier.notify(f"💸 {e}. Все задачи на паузе до /resume (или до нового дня/месяца).")

    async def _models_failed(self, tid: str, e: AllCandidatesFailed) -> None:
        aid = self.store.add_approval(tid, "escalation", {"issues": [str(e)]})
        self.store.update_task(tid, status="awaiting_owner", note="модели недоступны")
        self.store.add_step(tid, "error", "gateway", str(e))
        await self.notifier.notify(f"🛑 {tid}: ни одна модель не ответила корректно.\n{str(e)[:800]}",
                                   buttons_for(aid, [("Повторить", "rework"), ("Отменить", "cancel")]))
