"""Asynchronous coordinator for bounded, mediated multi-agent debates."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future, wait
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from ..constants import TaskStatus, TaskType
from ..runtime.tools.files import set_staging_root
from ..runtime.tools.web import fetch_url_text
from .commands import DebateCommand, DebateCommandError, parse_debate_command
from .models import (
    DebateContribution,
    DebateEvidence,
    DebateLimits,
    DebateMode,
    DebateProtocolError,
    DebateSession,
    DebateStatus,
    DebateSynthesis,
    WorkItem,
    contribution_from_response,
    synthesis_from_response,
)
from .prompts import (
    build_contribution_prompt,
    build_repair_prompt,
    build_synthesis_prompt,
)
from .repository import DebateRepository


class DebateCancelled(RuntimeError):
    pass


def _submit_daemon_future(
    name: str,
    function: Callable[..., Any],
    *args: Any,
) -> Future:
    """Run a cancellable external call without letting it pin process shutdown."""
    future: Future = Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = function(*args)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    threading.Thread(target=run, name=name, daemon=True).start()
    return future


@dataclass
class _CallHandle:
    cancel_event: threading.Event
    _lock: threading.Lock
    _cancel_fn: Callable[[], None] | None = None

    @classmethod
    def create(cls) -> "_CallHandle":
        return cls(threading.Event(), threading.Lock())

    def bind(self, cancel_fn: Callable[[], None] | None) -> None:
        with self._lock:
            self._cancel_fn = cancel_fn
            already_cancelled = self.cancel_event.is_set()
        if already_cancelled and callable(cancel_fn):
            cancel_fn()

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            cancel_fn = self._cancel_fn
        if callable(cancel_fn):
            try:
                cancel_fn()
            except Exception:
                pass


class DebateService:
    """Owns command handling, state transitions and debate child lifetimes."""

    DEFAULT_PARTICIPANTS = 3
    MAX_PARTICIPANTS = 5
    MAX_EVIDENCE_SOURCE_BYTES = 5_000_000
    MAX_WEB_EVIDENCE_CHARS = 2_000_000
    MIN_WEB_EXCERPT_CHARS = 24

    def __init__(
        self,
        *,
        repository: DebateRepository,
        task_repository: Any,
        dispatch_factory: Callable[[threading.Event], Any],
        active_agents: Callable[[], list[str]],
        renderer: Any,
        session_id: str,
        current_job_id: int,
        staging_root: Path,
        workspace_root: Path,
        persist_message: Callable[[str, str], Any] | None = None,
        notify_tasks_changed: Callable[[], None] | None = None,
        show_system: Callable[[str], None] | None = None,
        show_warning: Callable[[str], None] | None = None,
        show_error: Callable[[str], None] | None = None,
        web_fetcher: Callable[[str], str] | None = None,
    ) -> None:
        self._repository = repository
        self._task_repository = task_repository
        self._dispatch_factory = dispatch_factory
        self._active_agents = active_agents
        self._renderer = renderer
        self._session_id = str(session_id)
        self._current_job_id = int(current_job_id)
        self._staging_root = Path(staging_root)
        self._workspace_root = Path(workspace_root).resolve()
        self._persist_message = persist_message
        self._notify_tasks_changed = notify_tasks_changed or (lambda: None)
        self._show_system = show_system or (lambda _message: None)
        self._show_warning = show_warning or (lambda _message: None)
        self._show_error = show_error or (lambda _message: None)
        self._web_fetcher = web_fetcher or fetch_url_text
        self._repository.recover_expired()
        self._state_lock = threading.RLock()
        self._apply_lock = threading.Lock()
        self._active_id: str | None = None
        self._active_thread: threading.Thread | None = None
        self._root_cancel: threading.Event | None = None
        self._active_calls: dict[str, _CallHandle] = {}
        self._closed = False

    def handle_command(self, command: str) -> bool:
        try:
            parsed = parse_debate_command(command)
            if parsed.action == "start":
                session = self.start(parsed)
                self._show_system(
                    f"[debate {session.id}] iniciado com {len(session.participants)} agentes, "
                    f"{session.limits.max_rounds} rodada(s), modo {session.mode.value}."
                )
            elif parsed.action == "status":
                self._show_system(self.format_status(parsed.debate_id))
            elif parsed.action == "cancel":
                target = parsed.debate_id or self.active_id
                if self.cancel(target):
                    self._show_system(f"[debate {target}] cancelamento solicitado.")
                else:
                    self._show_warning("Nenhum debate ativo corresponde ao pedido.")
            elif parsed.action == "show":
                self._show_system(self.format_details(parsed.debate_id))
            elif parsed.action == "list":
                self._show_system(self.format_list())
            elif parsed.action == "apply":
                task_ids = self.apply(parsed.debate_id)
                self._show_system(
                    f"[debate {parsed.debate_id}] workflow aplicado em tasks: "
                    + ", ".join(str(task_id) for task_id in task_ids)
                )
            return True
        except (DebateCommandError, DebateProtocolError, ValueError, KeyError) as exc:
            self._show_warning(str(exc))
            return True
        except Exception as exc:
            self._show_error(f"Falha no comando /debate: {exc}")
            return True

    @property
    def active_id(self) -> str:
        with self._state_lock:
            return self._active_id or ""

    def start(self, command: DebateCommand) -> DebateSession:
        if command.action != "start":
            raise ValueError("comando de inicio esperado")
        participants = self._select_participants(command.agents)
        quorum = command.quorum or max(2, math.ceil(len(participants) * 2 / 3))
        if quorum > len(participants):
            raise ValueError("quorum nao pode exceder o numero de participantes")
        with self._state_lock:
            if self._closed:
                raise RuntimeError("servico de debates encerrado")
            if self._active_thread is not None and self._active_thread.is_alive():
                raise ValueError(f"ja existe um debate ativo: {self._active_id}")
            debate_id = f"deb-{uuid.uuid4().hex[:10]}"
            session = DebateSession(
                id=debate_id,
                session_id=self._session_id,
                topic=command.topic,
                mode=command.mode,
                status=DebateStatus.CREATED,
                participants=participants,
                moderator=participants[0],
                limits=DebateLimits(command.rounds, command.timeout_seconds, quorum),
            )
            self._repository.create_session(session)
            root_cancel = threading.Event()
            thread = threading.Thread(
                target=self._run_session_guarded,
                args=(session, root_cancel),
                name=f"debate-{debate_id}",
                daemon=True,
            )
            self._active_id = debate_id
            self._root_cancel = root_cancel
            self._active_thread = thread
            thread.start()
            return session

    def cancel(self, debate_id: str = "") -> bool:
        with self._state_lock:
            if (
                not self._active_id
                or not self._active_thread
                or not self._active_thread.is_alive()
            ):
                return False
            if debate_id and debate_id != self._active_id:
                return False
            root_cancel = self._root_cancel
            handles = tuple(self._active_calls.values())
        if root_cancel is not None:
            root_cancel.set()
        for handle in handles:
            handle.cancel()
        return True

    def cancel_active(self) -> None:
        self.cancel()

    def wait(self, timeout: float | None = None) -> bool:
        with self._state_lock:
            thread = self._active_thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._state_lock:
            self._closed = True
        self.cancel()
        self.wait(timeout)

    def format_status(self, debate_id: str = "") -> str:
        target = debate_id or self.active_id
        session = (
            self._repository.get_session(target)
            if target
            else self._repository.latest_session()
        )
        if session is None:
            return "Nenhum debate encontrado."
        return (
            f"[debate {session.id}] {session.status.value} | modo={session.mode.value} | "
            f"rodada={session.current_round}/{session.limits.max_rounds} | "
            f"quorum={session.limits.quorum} | agentes={', '.join(session.participants)}"
        )

    def format_details(self, debate_id: str) -> str:
        session = self._require_session(debate_id)
        lines = [self.format_status(debate_id), f"Tema: {session.topic}"]
        contributions = self._repository.list_contributions(debate_id)
        lines.append(f"Contribuicoes: {len(contributions)}")
        if session.result is not None:
            lines.extend(["", session.result.render(final=True, status=session.status)])
        if session.error:
            lines.append(f"Erro: {session.error}")
        if session.applied_at:
            links = self._repository.task_links(debate_id)
            lines.append(
                "Tasks: " + ", ".join(str(task_id) for task_id in links.values())
            )
        return "\n".join(lines)

    def format_list(self) -> str:
        sessions = self._repository.list_sessions(limit=20)
        if not sessions:
            return "Nenhum debate encontrado."
        return "\n".join(
            f"- {session.id} | {session.status.value} | {session.mode.value} | {session.topic[:80]}"
            for session in sessions
        )

    def apply(self, debate_id: str) -> list[int]:
        with self._apply_lock:
            session = self._require_session(debate_id)
            if not session.terminal or session.status in {
                DebateStatus.CANCELLED,
                DebateStatus.FAILED,
            }:
                raise ValueError("somente debates concluidos podem ser aplicados")
            if session.mode != DebateMode.WORKFLOW:
                raise ValueError("/debate apply exige um debate em modo workflow")
            items = self._repository.get_work_items(debate_id)
            if not items:
                raise ValueError("o debate nao produziu work_items")
            active = set(self._normalized_active_agents())
            links = self._repository.task_links(debate_id)
            tasks_changed = False

            for item in items:
                source_context = f"debate:{debate_id}:work-item:{item.id}"
                task_id = links.get(item.id)
                if task_id is None:
                    existing = self._task_repository.find_task_by_source_context(
                        source_context
                    )
                    if existing is not None:
                        task_id = existing.id
                    else:
                        assigned_to = (
                            item.assigned_to if item.assigned_to in active else None
                        )
                        try:
                            task_id = self._task_repository.create_task(
                                self._current_job_id,
                                item.description,
                                task_type=_task_type(item.task_type),
                                assigned_to=assigned_to,
                                origin="debate",
                                status=TaskStatus.PENDING,
                                priority=item.priority,
                                created_by="debate",
                                requested_by="human",
                                notes=_work_item_notes(item),
                                body=json.dumps(
                                    {
                                        "debate_id": debate_id,
                                        "work_item": item.as_dict(),
                                    },
                                    ensure_ascii=False,
                                ),
                                source_context=source_context,
                            )
                            tasks_changed = True
                        except sqlite3.IntegrityError:
                            existing = (
                                self._task_repository.find_task_by_source_context(
                                    source_context
                                )
                            )
                            if existing is None:
                                raise
                            task_id = existing.id
                    self._repository.link_task(debate_id, item.id, int(task_id))
                    links[item.id] = int(task_id)

            for item in items:
                task_id = links[item.id]
                for dependency in item.dependencies:
                    tasks_changed = (
                        self._task_repository.add_task_dependency(
                            task_id, links[dependency]
                        )
                        or tasks_changed
                    )

            self._repository.mark_applied(debate_id)
            if tasks_changed:
                self._notify_tasks_changed()
            return [links[item.id] for item in items]

    def _run_session_guarded(
        self, session: DebateSession, root_cancel: threading.Event
    ) -> None:
        try:
            self._run_session(session, root_cancel)
        except DebateCancelled:
            self._repository.set_status(session.id, DebateStatus.CANCELLED)
            self._show_system(f"[debate {session.id}] cancelado.")
        except Exception as exc:
            self._repository.set_status(session.id, DebateStatus.FAILED, error=str(exc))
            self._show_error(f"[debate {session.id}] falhou: {exc}")
        finally:
            with self._state_lock:
                if self._active_id == session.id:
                    self._active_id = None
                    self._root_cancel = None
                    self._active_calls.clear()

    def _run_session(
        self, session: DebateSession, root_cancel: threading.Event
    ) -> None:
        self._repository.set_status(session.id, DebateStatus.RUNNING, current_round=0)
        previous: tuple[DebateContribution, ...] = ()
        candidate: DebateSynthesis | None = None
        deadline = time.monotonic() + session.limits.timeout_seconds

        for round_index in range(1, session.limits.max_rounds + 1):
            self._raise_if_cancelled(root_cancel)
            remaining = self._remaining_seconds(session, deadline)
            self._repository.set_status(
                session.id,
                DebateStatus.RUNNING,
                current_round=round_index,
            )
            contributions, failures = self._run_round(
                session,
                round_index=round_index,
                previous=previous,
                candidate=candidate,
                root_cancel=root_cancel,
                timeout_seconds=remaining,
            )
            if len(contributions) < session.limits.quorum:
                detail = "; ".join(
                    f"{agent}: {error}" for agent, error in failures.items()
                )
                raise RuntimeError(
                    f"quorum nao atingido na rodada {round_index} "
                    f"({len(contributions)}/{session.limits.quorum}){': ' + detail if detail else ''}"
                )
            for contribution in contributions:
                self._repository.add_contribution(contribution)

            if candidate is not None and self._has_consensus(
                contributions, session.limits.quorum
            ):
                final = self._finalize_candidate(
                    candidate,
                    contributions,
                    round_index,
                    consensus_reached=True,
                )
                self._repository.add_synthesis(final)
                self._repository.set_status(
                    session.id,
                    DebateStatus.CONVERGED,
                    current_round=round_index,
                    result=final,
                )
                self._render_agent(
                    session.moderator,
                    final.render(final=True, status=DebateStatus.CONVERGED),
                )
                self._persist_final(session.moderator, final, DebateStatus.CONVERGED)
                return

            self._repository.set_status(
                session.id,
                DebateStatus.SYNTHESIZING,
                current_round=round_index,
            )
            candidate = self._run_synthesis(
                session,
                round_index=round_index,
                contributions=contributions,
                previous_candidate=candidate,
                root_cancel=root_cancel,
                timeout_seconds=self._remaining_seconds(session, deadline),
            )
            self._repository.add_synthesis(candidate)

            if round_index == session.limits.max_rounds:
                final = self._finalize_candidate(
                    candidate,
                    contributions,
                    round_index,
                    consensus_reached=False,
                )
                self._repository.set_status(
                    session.id,
                    DebateStatus.EXHAUSTED,
                    current_round=round_index,
                    result=final,
                )
                self._render_agent(
                    session.moderator,
                    final.render(final=True, status=DebateStatus.EXHAUSTED),
                )
                self._persist_final(session.moderator, final, DebateStatus.EXHAUSTED)
                return

            self._render_agent(session.moderator, candidate.render())
            previous = contributions

    def _run_round(
        self,
        session: DebateSession,
        *,
        round_index: int,
        previous: tuple[DebateContribution, ...],
        candidate: DebateSynthesis | None,
        root_cancel: threading.Event,
        timeout_seconds: float,
    ) -> tuple[tuple[DebateContribution, ...], dict[str, str]]:
        futures: dict[Future, tuple[str, _CallHandle]] = {}
        try:
            for agent in session.participants:
                handle = _CallHandle.create()
                key = f"r{round_index}:{agent}"
                self._register_call(key, handle, root_cancel)
                future = _submit_daemon_future(
                    f"debate-{session.id}-r{round_index}-{agent}",
                    self._run_participant,
                    session,
                    round_index,
                    agent,
                    previous,
                    candidate,
                    handle,
                    root_cancel,
                )
                futures[future] = (agent, handle)
            done, pending = wait(tuple(futures), timeout=timeout_seconds)
            for future in pending:
                futures[future][1].cancel()
            contributions: list[DebateContribution] = []
            failures: dict[str, str] = {}
            for future in done:
                agent, _ = futures[future]
                try:
                    contributions.append(future.result())
                except DebateCancelled:
                    if root_cancel.is_set():
                        raise
                    failures[agent] = "cancelado"
                except Exception as exc:
                    failures[agent] = str(exc)
            for future in pending:
                agent, _ = futures[future]
                failures[agent] = f"timeout apos {timeout_seconds:.1f}s"
            self._raise_if_cancelled(root_cancel)
            if pending:
                raise RuntimeError(
                    f"debate excedeu timeout total de {session.limits.timeout_seconds:.0f}s"
                )
            order = {agent: index for index, agent in enumerate(session.participants)}
            contributions.sort(key=lambda item: order[item.agent])
            return tuple(contributions), failures
        finally:
            for future, (_, handle) in futures.items():
                if root_cancel.is_set() or not future.done():
                    handle.cancel()
            for _, (agent, _) in futures.items():
                self._unregister_call(f"r{round_index}:{agent}")

    def _run_participant(
        self,
        session: DebateSession,
        round_index: int,
        agent: str,
        previous: tuple[DebateContribution, ...],
        candidate: DebateSynthesis | None,
        handle: _CallHandle,
        root_cancel: threading.Event,
    ) -> DebateContribution:
        prompt = build_contribution_prompt(
            session,
            round_index=round_index,
            agent=agent,
            previous=previous,
            candidate=candidate,
        )
        raw = self._call_agent(
            session, round_index, agent, "participant", prompt, handle, root_cancel
        )
        try:
            contribution = contribution_from_response(
                raw,
                debate_id=session.id,
                round_index=round_index,
                agent=agent,
            )
            self._verify_evidence(contribution.evidence)
            self._validate_contribution(contribution, candidate)
        except DebateProtocolError as exc:
            repaired = self._call_agent(
                session,
                round_index,
                agent,
                "participant-repair",
                build_repair_prompt(prompt, raw, exc),
                handle,
                root_cancel,
            )
            contribution = contribution_from_response(
                repaired,
                debate_id=session.id,
                round_index=round_index,
                agent=agent,
            )
            self._verify_evidence(contribution.evidence)
            self._validate_contribution(contribution, candidate)
        self._render_agent(contribution.agent, contribution.render())
        return contribution

    def _run_synthesis(
        self,
        session: DebateSession,
        *,
        round_index: int,
        contributions: tuple[DebateContribution, ...],
        previous_candidate: DebateSynthesis | None,
        root_cancel: threading.Event,
        timeout_seconds: float,
    ) -> DebateSynthesis:
        handle = _CallHandle.create()
        key = f"r{round_index}:synthesis:{session.moderator}"
        self._register_call(key, handle, root_cancel)
        prompt = build_synthesis_prompt(
            session,
            round_index=round_index,
            contributions=contributions,
            previous_candidate=previous_candidate,
        )
        future = _submit_daemon_future(
            f"debate-{session.id}-synthesis",
            self._synthesis_worker,
            session,
            round_index,
            contributions,
            root_cancel,
            handle,
            prompt,
        )
        try:
            done, _ = wait((future,), timeout=timeout_seconds)
            if not done:
                handle.cancel()
                raise RuntimeError(
                    f"debate excedeu timeout total de {session.limits.timeout_seconds:.0f}s"
                )
            return future.result()
        finally:
            if root_cancel.is_set() or not future.done():
                handle.cancel()
            self._unregister_call(key)

    def _synthesis_worker(
        self,
        session: DebateSession,
        round_index: int,
        contributions: tuple[DebateContribution, ...],
        root_cancel: threading.Event,
        handle: _CallHandle,
        prompt: str,
    ) -> DebateSynthesis:
        raw = self._call_agent(
            session,
            round_index,
            session.moderator,
            "synthesis",
            prompt,
            handle,
            root_cancel,
        )
        try:
            synthesis = synthesis_from_response(
                raw,
                debate_id=session.id,
                round_index=round_index,
                moderator=session.moderator,
            )
            self._verify_evidence(synthesis.evidence)
            self._validate_synthesis(session, synthesis)
            return synthesis
        except DebateProtocolError as exc:
            repaired = self._call_agent(
                session,
                round_index,
                session.moderator,
                "synthesis-repair",
                build_repair_prompt(prompt, raw, exc),
                handle,
                root_cancel,
            )
            synthesis = synthesis_from_response(
                repaired,
                debate_id=session.id,
                round_index=round_index,
                moderator=session.moderator,
            )
            self._verify_evidence(synthesis.evidence)
            self._validate_synthesis(session, synthesis)
            return synthesis

    def _call_agent(
        self,
        session: DebateSession,
        round_index: int,
        agent: str,
        purpose: str,
        prompt: str,
        handle: _CallHandle,
        root_cancel: threading.Event,
    ) -> str:
        self._raise_if_cancelled(root_cancel)
        if handle.cancel_event.is_set():
            raise DebateCancelled()
        staging = (
            self._staging_root / session.id / f"round-{round_index}" / agent / purpose
        )
        set_staging_root(staging)
        dispatch = None
        try:
            dispatch = self._dispatch_factory(handle.cancel_event)
            if dispatch is None:
                raise RuntimeError("dispatch isolado indisponivel")
            get_client = getattr(dispatch, "_get_agent_client", None)
            client = get_client() if callable(get_client) else None
            handle.bind(getattr(client, "cancel_active_work", None))
            raw = dispatch.delegate(
                agent,
                delegation={
                    "delegation_id": f"{session.id}:r{round_index}:{purpose}:{agent}",
                    "parent_run_id": f"debate:{session.id}",
                    "from_agent": "debate",
                    "task": session.topic[:240],
                },
                from_agent="debate",
                primary=False,
                protocol_mode="debate",
                delegation_only=True,
                silent=True,
                show_output=False,
                show_delegation=False,
                persist_history=False,
                history_snapshot=[],
                request_override=prompt,
                max_retries=0,
                emit_run_deltas=False,
            )
            if handle.cancel_event.is_set() or root_cancel.is_set():
                raise DebateCancelled()
            if raw is None or not str(raw).strip():
                raise RuntimeError(f"{agent} nao retornou resposta")
            return str(raw)
        finally:
            handle.bind(None)
            if dispatch is not None:
                close = getattr(dispatch, "close", None)
                if callable(close):
                    close()
            set_staging_root(None)

    def _register_call(
        self,
        key: str,
        handle: _CallHandle,
        root_cancel: threading.Event,
    ) -> None:
        with self._state_lock:
            self._active_calls[key] = handle
            cancelled = root_cancel.is_set()
        if cancelled:
            handle.cancel()

    def _unregister_call(self, key: str) -> None:
        with self._state_lock:
            self._active_calls.pop(key, None)

    def _select_participants(self, requested: tuple[str, ...]) -> tuple[str, ...]:
        active = self._normalized_active_agents()
        if requested:
            missing = [agent for agent in requested if agent not in active]
            if missing:
                raise ValueError("agentes nao ativos: " + ", ".join(missing))
            participants = requested
        else:
            participants = tuple(active[: self.DEFAULT_PARTICIPANTS])
        if len(participants) < 2:
            raise ValueError("/debate exige pelo menos 2 agentes ativos")
        if len(participants) > self.MAX_PARTICIPANTS:
            raise ValueError(
                f"/debate aceita no maximo {self.MAX_PARTICIPANTS} agentes"
            )
        return tuple(participants)

    def _normalized_active_agents(self) -> list[str]:
        return list(
            dict.fromkeys(
                str(agent).strip().lower().lstrip("/")
                for agent in self._active_agents()
                if str(agent).strip() and str(agent).strip() != "*"
            )
        )

    @staticmethod
    def _validate_contribution(
        contribution: DebateContribution,
        candidate: DebateSynthesis | None,
    ) -> None:
        if candidate is None and contribution.vote != "propose":
            raise DebateProtocolError("primeira rodada exige vote='propose'")
        if candidate is not None and contribution.vote == "propose":
            raise DebateProtocolError(
                "rodadas com candidato exigem support, oppose ou abstain"
            )

    @staticmethod
    def _has_consensus(
        contributions: tuple[DebateContribution, ...], quorum: int
    ) -> bool:
        support = sum(1 for item in contributions if item.vote == "support")
        critical = any(item.critical_objection for item in contributions)
        return support >= quorum and not critical

    @staticmethod
    def _finalize_candidate(
        candidate: DebateSynthesis,
        contributions: tuple[DebateContribution, ...],
        round_index: int,
        *,
        consensus_reached: bool,
    ) -> DebateSynthesis:
        dissent = list(candidate.disagreements)
        critical = list(candidate.critical_objections)
        confidences = [candidate.confidence]
        for contribution in contributions:
            confidences.append(contribution.confidence)
            if contribution.vote != "support":
                dissent.extend(contribution.objections or (contribution.position,))
            if contribution.critical_objection:
                critical.extend(contribution.objections or (contribution.position,))
        return replace(
            candidate,
            round_index=round_index,
            disagreements=tuple(dict.fromkeys(item for item in dissent if item)),
            critical_objections=tuple(dict.fromkeys(item for item in critical if item)),
            confidence=sum(confidences) / len(confidences),
            consensus_reached=consensus_reached,
        )

    @staticmethod
    def _validate_synthesis(session: DebateSession, synthesis: DebateSynthesis) -> None:
        if session.mode == DebateMode.WORKFLOW and not synthesis.work_items:
            raise DebateProtocolError("sintese workflow nao contem work_items")
        participants = set(session.participants)
        invalid = sorted(
            {
                item.assigned_to
                for item in synthesis.work_items
                if item.assigned_to and item.assigned_to not in participants
            }
        )
        if invalid:
            raise DebateProtocolError(
                "work_items atribuidos a agentes invalidos: " + ", ".join(invalid)
            )

    def _verify_evidence(self, evidence_items: tuple[DebateEvidence, ...]) -> None:
        """Verify cited excerpts against immutable reads (workspace or web)."""
        web_cache: dict[str, str] = {}
        for evidence in evidence_items:
            if evidence.kind == "web":
                self._verify_web_evidence(evidence, web_cache)
                continue
            source = (self._workspace_root / evidence.source).resolve()
            try:
                source.relative_to(self._workspace_root)
            except ValueError as exc:
                raise DebateProtocolError(
                    f"evidence {evidence.id} aponta para fora do workspace"
                ) from exc
            if self._is_sensitive_evidence_path(source):
                raise DebateProtocolError(
                    f"evidence {evidence.id} aponta para arquivo sensivel"
                )
            if not source.is_file():
                raise DebateProtocolError(
                    f"evidence {evidence.id} aponta para arquivo inexistente: {evidence.source}"
                )
            try:
                if source.stat().st_size > self.MAX_EVIDENCE_SOURCE_BYTES:
                    raise DebateProtocolError(
                        f"evidence {evidence.id} aponta para arquivo grande demais"
                    )
            except OSError as exc:
                raise DebateProtocolError(
                    f"evidence {evidence.id} nao pode ser inspecionada: {evidence.source}"
                ) from exc
            selected: list[str] = []
            last_line = 0
            try:
                with source.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if line_number > evidence.line_end:
                            break
                        last_line = line_number
                        if line_number >= evidence.line_start:
                            selected.append(line)
            except OSError as exc:
                raise DebateProtocolError(
                    f"evidence {evidence.id} nao pode ser lida: {evidence.source}"
                ) from exc
            if last_line < evidence.line_end or not selected:
                raise DebateProtocolError(
                    f"evidence {evidence.id} cita linhas fora do arquivo: {evidence.source}"
                )
            excerpt = self._normalize_evidence_text(evidence.excerpt)
            actual = self._normalize_evidence_text("".join(selected))
            if len(excerpt) < 8 or excerpt not in actual:
                raise DebateProtocolError(
                    f"evidence {evidence.id} nao corresponde ao trecho citado em "
                    f"{evidence.source}:{evidence.line_start}-{evidence.line_end}"
                )

    def _verify_web_evidence(
        self, evidence: DebateEvidence, cache: dict[str, str]
    ) -> None:
        """Refetch the cited URL and require the excerpt in the live page text."""
        excerpt = self._normalize_evidence_text(evidence.excerpt)
        if len(excerpt) < self.MIN_WEB_EXCERPT_CHARS:
            raise DebateProtocolError(
                f"evidence {evidence.id} tem excerpt curto demais para "
                f"verificacao web (minimo {self.MIN_WEB_EXCERPT_CHARS} caracteres)"
            )
        if evidence.source not in cache:
            try:
                page = self._web_fetcher(evidence.source)
            except Exception as exc:
                raise DebateProtocolError(
                    f"evidence {evidence.id} nao pode ser verificada; "
                    f"falha ao buscar {evidence.source}: {exc}"
                ) from exc
            cache[evidence.source] = self._normalize_evidence_text(
                str(page or "")[: self.MAX_WEB_EVIDENCE_CHARS]
            )
        if excerpt not in cache[evidence.source]:
            raise DebateProtocolError(
                f"evidence {evidence.id} nao corresponde ao conteudo atual de "
                f"{evidence.source}"
            )

    @staticmethod
    def _normalize_evidence_text(value: str) -> str:
        return " ".join(str(value or "").split())

    @staticmethod
    def _is_sensitive_evidence_path(path: Path) -> bool:
        lowered_parts = {part.lower() for part in path.parts}
        name = path.name.lower()
        return (
            ".git" in lowered_parts
            or name.startswith(".env")
            or name
            in {
                ".netrc",
                ".npmrc",
                ".pypirc",
                "credentials.json",
                "secrets.json",
                "id_rsa",
                "id_ed25519",
            }
            or path.suffix.lower()
            in {".pem", ".key", ".p12", ".pfx", ".keystore"}
        )

    def _render_agent(self, agent: str, content: str) -> None:
        if self._renderer is None or not content.strip():
            return
        show = getattr(self._renderer, "show_message", None)
        if callable(show):
            show(agent, content)
        flush = getattr(self._renderer, "flush_quick", None) or getattr(
            self._renderer, "flush", None
        )
        if callable(flush):
            flush()

    def _persist_final(
        self,
        moderator: str,
        synthesis: DebateSynthesis,
        status: DebateStatus,
    ) -> None:
        if callable(self._persist_message):
            self._persist_message(
                moderator, synthesis.render(final=True, status=status)
            )

    def _require_session(self, debate_id: str) -> DebateSession:
        session = self._repository.get_session(str(debate_id or "").strip())
        if session is None:
            raise KeyError(f"debate inexistente: {debate_id}")
        return session

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise DebateCancelled()

    @staticmethod
    def _remaining_seconds(session: DebateSession, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"debate excedeu timeout total de {session.limits.timeout_seconds:.0f}s"
            )
        return remaining


def _task_type(value: str) -> TaskType:
    try:
        return TaskType(str(value or "general"))
    except ValueError:
        return TaskType.GENERAL


def _work_item_notes(item: WorkItem) -> str:
    notes = []
    if item.dependencies:
        notes.append("Dependencias: " + ", ".join(item.dependencies))
    if item.acceptance_criteria:
        notes.append("Criterios de aceite: " + "; ".join(item.acceptance_criteria))
    return "\n".join(notes)
