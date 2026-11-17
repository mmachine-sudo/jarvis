"""Оркестратор: жизненный цикл задачи с чекпоинтами после каждого этапа.

new → (awaiting_clarification) → ready → executing → verifying → done
                                              ↑            │
                                              └── rework ←─┘ (≤ max_rounds, затем awaiting_owner)
Класс A и любые действия владельца всегда заканчиваются awaiting_owner.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

from .agent.cases import Cases
from .agent.prompts import RESUME_ACTION, RESUME_ANSWER, RESUME_RESTART
from .agent.runner import AgentRunner, RunState

from .card import Privacy, Result, TaskCard, TaskClass
from .config import Settings
from .gateway import AllCandidatesFailed, Gateway, LocalOnlyGateway, ProviderPaused, Reply
from .guard import Guard
from .intake import draft_card
from .reminders import Reminders
from .rework import Rework, place_of
from .router import is_reminder
from .router import needs_local
from .journal import Journal
from .memory import Memory
from .modes import ExecContext, run_solo, run_team
from .reader import QuarantineReader, Searcher
from .store import Store
from .treasury import BudgetExceeded
from .verify import accepted, checklist, consilium_review, critic_review, official_source_gap

log = logging.getLogger("jarvis.orch")

ACTIVE = ["new", "ready", "executing", "verifying", "rework", "agent_queued", "agent_resume"]
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
        self.reminders = Reminders(store, settings.owner.timezone)
        self.cases = Cases(store)
        self.agent: AgentRunner | None = None  # ставит app.build: нужен казначей, поиск и бот
        self._bg: dict[str, asyncio.Task] = {}

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
        if status == "ready" and self._goes_to_agent(t):
            self._queue_agent(t)
            return
        if status in ("agent_queued", "agent_resume"):
            self._start_agent(t)
            return
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
        if is_reminder(t["request"]) and not needs_local(t["request"], self.s.router):
            text, when = await self.reminders.create(tid, t["request"], self.gw, self.s.task_cap("C"))
            self.store.update_task(tid, status="done", cls="C", note=f"напоминание на {when:%d.%m %H:%M}")
            self.store.add_step(tid, "done", "jarvis", f"напоминание {when:%Y-%m-%d %H:%M}: {text}")
            await self.notifier.notify(f"⏰ {tid}: напомню {when:%d.%m в %H:%M} — {text}")
            return
        answers = Store.loads(t["answers_json"], [])
        allow_q = not answers and not Store.loads(t["questions_json"], None)
        if allow_q:
            # повтор похожей задачи: владелец уже отвечал на уточнения — не спрашиваем заново, берём его ответы
            prior = self.cases.prior_answers(t["request"], exclude=tid)
            if prior:
                src, answers = prior[0], [f"(из {prior[0]}) {a}" for a in prior[1]]
                self.store.update_task(tid, answers_json=answers)
                self.store.add_step(tid, "answer", "jarvis", f"ответы на уточнения взяты из похожей задачи {src}")
                await self.notifier.notify(f"↩️ {tid}: похожая задача уже была ({src}) — беру твои ответы оттуда, "
                                           f"не спрашиваю заново. Если что-то изменилось — /answer {tid} текст.")
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
                          reader=self.reader, searcher=self.searcher,
                          prior_findings=Store.loads(t["result_json"], {}).get("findings", []))
        result = await (run_solo(ctx) if card.mode.value == "solo" else run_team(ctx))
        self.store.update_task(tid, status="verifying", result_json={"result": result.model_dump(),
                                                                     "author": ctx.author,
                                                                     "findings": ctx.findings,
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

    # --- агентное ядро v5 ------------------------------------------------------------------------
    def _goes_to_agent(self, t: dict) -> bool:
        card = self.card_of(t)
        return bool(self.agent and self.s.agent.enabled and card and card.cls.value in self.s.agent.classes
                    and card.privacy != Privacy.LOCAL_ONLY)

    def _payload(self, t: dict) -> dict:
        return Store.loads(t["result_json"], {}) or {}

    def _set_payload(self, tid: str, payload: dict, **fields) -> None:
        self.store.update_task(tid, result_json=json.dumps(payload, ensure_ascii=False), **fields)

    def _queue_agent(self, t: dict) -> None:
        card = self.card_of(t)
        answers = Store.loads(t["answers_json"], []) or []
        prompt = (f"Задача {t['id']} от владельца:\n{t['request']}\n\n"
                  f"Разбор при приёме: цель — {card.goal}; образ результата — {card.result_image}; "
                  f"критерии — {'; '.join(card.acceptance)}; класс {card.cls.value} "
                  f"({'деньги/право/необратимое' if card.cls.value == 'A' else 'важное, обратимое'}).")
        if answers:
            prompt += "\nОтветы владельца на уточнения при приёме: " + " | ".join(answers)
        self.cases.open(t["id"], card.goal, t["request"])
        payload = self._payload(t)
        payload["next_prompt"] = prompt
        self._set_payload(t["id"], payload, status="agent_queued")
        self.store.add_step(t["id"], "route", "jarvis", "задача отдана агентному ядру (v5)")

    def _start_agent(self, t: dict) -> None:
        tid = t["id"]
        if tid in self._bg and not self._bg[tid].done():
            return
        # агенты по очереди: общий ключ и общий счётчик трат, деньги не разбегаются параллельно
        if any(k != tid and not v.done() for k, v in self._bg.items()):
            return
        payload = self._payload(t)
        prompt = payload.pop("next_prompt", None) or RESUME_RESTART
        self._set_payload(tid, payload, status="agent_running")
        self._bg[tid] = asyncio.create_task(self._agent_cycle(tid, prompt))

    def recover_agents(self) -> int:
        """После перезапуска: прерванные прогоны продолжаются той же сессией."""
        n = 0
        for t in self.store.tasks(statuses=["agent_running"], limit=50):
            payload = self._payload(t)
            payload["next_prompt"] = RESUME_RESTART
            self._set_payload(t["id"], payload, status="agent_resume")
            n += 1
        return n

    async def _agent_cycle(self, tid: str, prompt: str) -> None:
        try:
            st = await self.agent.run(tid, prompt)
        except BudgetExceeded as e:  # дневной/месячный лимит владельца
            self.store.update_task(tid, status="agent_resume")
            payload = self._payload(self.store.task(tid))
            payload["next_prompt"] = RESUME_RESTART
            self._set_payload(tid, payload)
            await self._budget_exceeded(tid, e)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("агентный прогон %s", tid)
            st = RunState()
            payload = self._payload(self.store.task(tid))
            payload["agent_error"] = f"{type(e).__name__}: {e}"[:300]
            self._set_payload(tid, payload)
        await self._after_agent(tid, st)

    async def _after_agent(self, tid: str, st: RunState) -> None:
        t = self.store.task(tid)
        payload = self._payload(t)
        spent = self.agent.spent(tid)
        if st.question:
            payload["pending"] = {"type": "question", **st.question}
            self._set_payload(tid, payload, status="agent_waiting", questions_json=st.question["questions"])
            qs = "\n".join(f"{i}. {q}" for i, q in enumerate(st.question["questions"], 1))
            await self.notifier.notify(f"❓ {tid} — вопросы перед работой ({st.question.get('why', '')}):\n{qs}\n\n"
                                       f"Ответь одним сообщением (или /answer {tid} текст).")
            return
        if st.action:
            payload["pending"] = {"type": "action", **st.action}
            aid = self.store.add_approval(tid, "agent_action", st.action)
            self._set_payload(tid, payload, status="agent_waiting")
            await self.notifier.notify(
                f"🛠 {tid} — нужно подтверждение: {st.action['title']}\n{st.action['details'][:1500]}\n"
                f"Стоимость: ${st.action['cost_usd']:.2f}", buttons_for(aid, [("Подтвердить", "approve"),
                                                                              ("Отклонить", "decline")]))
            return
        if st.finished:
            await self._agent_done(tid, st.finished, spent)
            return
        if payload.get("agent_error") in ("budget", "error_max_budget_usd") or spent >= self.s.agent.max_usd_per_task * float(t.get("cap_multiplier") or 1) - 0.05:
            aid = self.store.add_approval(tid, "budget", {"resume_status": "agent_resume"})
            payload["next_prompt"] = ("Лимит задачи увеличен владельцем. Продолжай с места остановки; если результат "
                                      "уже достаточен — сохрани RESULT.md и вызови finish.")
            self._set_payload(tid, payload, status="awaiting_owner")
            await self.notifier.notify(f"💸 {tid}: потрачен лимит ${spent:.2f}. Промежуточные файлы сохранены.",
                                       buttons_for(aid, [("Удвоить лимит задачи", "extend"), ("Остановить", "cancel")]))
            return
        nudges = int(payload.get("nudges", 0))
        if not payload.get("agent_error") and nudges < self.s.agent.max_nudges:  # закончил ход без finish
            payload.update(nudges=nudges + 1, next_prompt="Ты закончил ход, не вызвав finish. Если работа не "
                           "закончена — продолжай по методу; если закончена — сохрани RESULT.md и вызови finish.")
            self._set_payload(tid, payload, status="agent_resume")
            return
        aid = self.store.add_approval(tid, "escalation", {"issues": [payload.get("agent_error") or "агент остановился без итога"]})
        payload["next_prompt"] = RESUME_RESTART
        self._set_payload(tid, payload, status="awaiting_owner")
        await self.notifier.notify(f"⚠️ {tid}: агент остановился ({payload.get('agent_error') or 'без итога'}). "
                                   f"Потрачено ${spent:.2f}.", buttons_for(aid, [("Продолжить", "rework"), ("Отменить", "cancel")]))

    async def _agent_done(self, tid: str, fin: dict, spent: float) -> None:
        t = self.store.task(tid)
        payload = self._payload(t)
        path = self.agent.workdir(tid) / "RESULT.md"
        body = path.read_text(encoding="utf-8")[:60000] if path.exists() else (fin.get("summary") or "")
        conf = fin.get("confidence")
        self.cases.update(tid, strategy=str(fin.get("strategy") or "")[:3000], alternatives=fin.get("alternatives") or [],
                          result_summary=str(fin.get("summary") or "")[:4000], result_path=str(path),
                          confidence=float(conf) if isinstance(conf, (int, float)) else None, status="delivered")
        self.cases.add_lessons(tid, [str(x) for x in (fin.get("lessons") or [])], "самоанализ агента")
        payload.pop("pending", None)
        payload["result"] = {"answer": body, "owner_actions": [str(x) for x in (fin.get("owner_actions") or [])],
                             "open_questions": [str(x) for x in (fin.get("open_questions") or [])],
                             "jarvis_actions": [str(x) for x in (fin.get("jarvis_actions") or [])]}
        self._set_payload(tid, payload, status="done", note=f"агент: ${spent:.2f}, уверенность {conf}")
        self.store.add_step(tid, "done", "agent", f"готово, ${spent:.2f}")
        mine = "\n".join(f"• {x}" for x in payload["result"]["owner_actions"][:8])
        his = "\n".join(f"• {x}" for x in payload["result"]["jarvis_actions"][:8])
        text = (f"✅ {tid} готово (${spent:.2f}, уверенность {conf}).\n\n{str(fin.get('summary') or '')[:2500]}"
                + (f"\n\nЧто делаешь ты:\n{mine}" if mine else "")
                + (f"\n\nЧто могу сделать я (по твоему подтверждению):\n{his}" if his else "")
                + f"\n\nПолный результат — файлом ниже и в панели. Когда будет реальный исход — напиши "
                  f"/outcome {tid} что получилось: я разберу и запомню уроки.")
        await self.notifier.notify(text)
        send = getattr(self.notifier, "send_file", None)
        if send and path.exists():
            await send(str(path), f"{tid} — результат")

    async def outcome(self, tid: str, text: str) -> str:
        if not self.agent or not self.cases.get(tid):
            return "По этой задаче нет дела агента."
        self.store.add_step(tid, "outcome", "owner", text[:300])
        lessons = await self.agent.reflect(tid, text)
        return (f"Записал исход по {tid}. Уроки на будущее:\n" + "\n".join(f"• {x}" for x in lessons)) if lessons \
            else f"Записал исход по {tid}."

    def answer_clarification(self, tid: str, text: str) -> bool:
        t = self.store.task(tid)
        if t and t["status"] == "agent_waiting" and (self._payload(t).get("pending") or {}).get("type") == "question":
            payload = self._payload(t)
            payload.pop("pending", None)
            payload["next_prompt"] = RESUME_ANSWER.format(answer=text)
            self._set_payload(tid, payload, status="agent_resume", questions_json=[])
            self.store.add_step(tid, "answer", "owner", text[:300])
            return True
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
        if kind == "agent_action":
            verdict = "подтвердил" if choice == "approve" else "отклонил"
            pl = self._payload(t)
            pl.pop("pending", None)
            pl["next_prompt"] = RESUME_ACTION.format(verdict=verdict, title=payload.get("title", ""),
                                                     note="" if choice == "approve" else " Ищи путь без этого действия.")
            self._set_payload(tid, pl, status="agent_resume")
            return f"{tid}: {verdict}, агент продолжает."
        if kind == "escalation" and choice == "rework" and self._payload(t).get("session_id"):
            pl = self._payload(t)
            pl["next_prompt"] = pl.get("next_prompt") or RESUME_RESTART
            pl["nudges"] = 0
            self._set_payload(tid, pl, status="agent_resume")
            return f"{tid}: агент продолжает."
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
