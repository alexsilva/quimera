"""Modelo lógico do feed da UI Textual.

Este módulo não depende de Textual nem de widgets. Ele recebe eventos semânticos
e decide como o scrollback lógico deve mudar: anexar itens persistentes,
substituir estados transitórios e limpar previews.
"""
from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from itertools import count
from typing import Any, Callable, ClassVar

from quimera.app.submission_tracker import TERMINAL_SUBMISSION_STATUSES
from quimera.ui.commands import USER_ROLE
from quimera.ui.messages import AGENT_EXECUTION_STARTED_MESSAGE, format_duration
from quimera.ui.text import strip_ansi
from quimera.ui.textual.events import TextualUiEvent

_logger = logging.getLogger(__name__)
_feed_item_sequences = count(1)


class AgentLifecycleStatus(str, Enum):
    """Status estruturado de lifecycle de agente no feed Textual."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"
    ABORTED = "aborted"
    RETRYING = "retrying"
    RECONNECTING = "reconnecting"


RUN_BOUNDARY_LIFECYCLE_STATUSES = frozenset({
    AgentLifecycleStatus.COMPLETED,
    AgentLifecycleStatus.FAILED,
    AgentLifecycleStatus.ERROR,
    AgentLifecycleStatus.CANCELLED,
    AgentLifecycleStatus.ABORTED,
    AgentLifecycleStatus.RETRYING,
    AgentLifecycleStatus.RECONNECTING,
})


def _coerce_lifecycle_status(value: object) -> AgentLifecycleStatus:
    """Normaliza status de lifecycle para tipo estruturado."""
    if isinstance(value, AgentLifecycleStatus):
        return value
    normalized = str(value or "").strip().lower()
    aliases = {
        "done": AgentLifecycleStatus.COMPLETED,
        "finished": AgentLifecycleStatus.COMPLETED,
        "complete": AgentLifecycleStatus.COMPLETED,
        "completed": AgentLifecycleStatus.COMPLETED,
        "running": AgentLifecycleStatus.RUNNING,
        "failed": AgentLifecycleStatus.FAILED,
        "failure": AgentLifecycleStatus.FAILED,
        "error": AgentLifecycleStatus.ERROR,
        "cancelled": AgentLifecycleStatus.CANCELLED,
        "canceled": AgentLifecycleStatus.CANCELLED,
        "aborted": AgentLifecycleStatus.ABORTED,
        "retrying": AgentLifecycleStatus.RETRYING,
        "reconnecting": AgentLifecycleStatus.RECONNECTING,
    }
    return aliases.get(normalized, AgentLifecycleStatus.RUNNING)


def _agent_lifecycle_payload(
    message: str,
    *,
    status: AgentLifecycleStatus | str = AgentLifecycleStatus.RUNNING,
) -> dict[str, str]:
    """Cria payload estruturado de lifecycle para a UI."""
    normalized_status = _coerce_lifecycle_status(status)
    return {"status": normalized_status.value, "message": str(message or "")}


@dataclass
class TextualFeedItem:
    """Item lógico do feed Textual."""

    event: TextualUiEvent
    transient: bool = False
    seq: int = dataclass_field(default_factory=_feed_item_sequences.__next__)


class TextualFeedModel:
    """Modelo testável do feed: transitórios por agente são substituíveis."""

    _TRANSIENT_KINDS: ClassVar[set[str]] = {"agent_update", "agent_lifecycle", "pending_input"}
    _MCP_HTTP_TOOL_MARKER = "◇"
    # Máximo de linhas de tools visíveis no bloco transitório; o excedente
    # colapsa na linha "⋮ +N ferramentas anteriores".
    _MAX_VISIBLE_TOOL_LINES = 5
    # Teto das linhas de tools guardadas na resposta final para o toggle; o
    # excedente vira a última linha "⋮ +N ferramentas anteriores".
    _MAX_FINAL_TOOL_LINES = 200
    # Mesma ordem de grandeza da memória de runs encerradas do renderer.
    _MAX_FINALIZED_AGENTS = 1024

    _IGNORED_KINDS: ClassVar[set[str]] = {
        "prompt",
        "prompt_clear",
        "input_active",
        "summarizing",
        "window_open",
        "window_clear",
        "theme_changed",
    }

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        tool_preview_dwell_seconds: float = 15.0,
    ) -> None:
        self._clock = clock
        self._tool_preview_dwell_seconds = max(0.0, float(tool_preview_dwell_seconds))
        self._items: list[TextualFeedItem] = []
        self._transient_index_by_agent: dict[str, int] = {}
        self._active_submissions_by_index: dict[int, TextualFeedItem] = {}
        # Resposta parcial já fixada no feed por agente (texto cumulativo e os
        # itens fixados): a próxima atualização é cumulativa e só o trecho novo
        # continua no transitório.
        self._kept_answer_by_agent: dict[str, tuple[str, list[TextualFeedItem]]] = {}
        self._transient_tools_by_agent: dict[str, list[str]] = {}
        # Linha de preview de cada chamada de tool (por ``tool_call_id``): o
        # cartão da delegação remove exatamente a preview da própria chamada.
        self._tool_call_line_by_agent: dict[str, dict[str, str]] = {}
        self._tool_request_lines_by_agent: dict[str, dict[str, str]] = {}
        self._tool_request_seen_at_by_agent: dict[str, dict[str, float]] = {}
        self._tool_request_expiry_by_agent: dict[str, dict[str, float]] = {}
        self._completed_tool_requests_by_agent: dict[str, set[str]] = {}
        self._mcp_http_tool_stats_by_agent: dict[str, dict[str, int]] = {}
        self._pending_turn_summary_by_agent: dict[str, TextualUiEvent] = {}
        # Cartões vivos por delegation_id, do mais antigo ao mais novo: uma
        # re-tentativa de task reusa o mesmo id (``task-{id}``) enquanto a
        # execução anterior ainda pode estar viva.
        self._delegation_slots: dict[str, list[str]] = {}
        # Execução delegada → cartão com que ela foi pareada ao aparecer.
        self._delegation_child_slot: dict[str, str] = {}
        # Execuções já encerradas, em ordem de encerramento: evento tardio de
        # uma delas não reabre cartão. Limitado como LRU porque cada run
        # deixa uma chave nova durante a sessão inteira.
        self._finalized_agents: OrderedDict[str, None] = OrderedDict()

    @property
    def items(self) -> list[TextualFeedItem]:
        """Snapshot dos itens atuais do feed."""
        return list(self._items)

    @property
    def has_transients(self) -> bool:
        """Retorna se existe algum slot transitório ativo."""
        return bool(self._transient_index_by_agent)

    def transient_items(self) -> list[tuple[int, TextualFeedItem]]:
        """Snapshot dos slots transitórios ativos ordenados pela posição visual."""
        transients: list[tuple[int, TextualFeedItem]] = []
        for index in sorted(set(self._transient_index_by_agent.values())):
            if 0 <= index < len(self._items):
                item = self._items[index]
                if item.transient:
                    transients.append((index, item))
        return transients

    def active_submission_items(self) -> list[tuple[int, TextualFeedItem]]:
        """Snapshot ordenado dos turnos ativos, sem varrer o scrollback a cada pulso."""
        return sorted(self._active_submissions_by_index.items())

    def _track_submission_item(self, index: int, item: TextualFeedItem) -> None:
        """Mantém o índice pequeno de turnos cuja esfera ainda precisa de animação."""
        self._active_submissions_by_index.pop(index, None)
        if item.event.kind != "user_message":
            return
        payload = item.event.payload if isinstance(item.event.payload, dict) else {}
        submission = payload.get("submission")
        if not isinstance(submission, dict):
            return
        status = str(submission.get("status") or "").strip().lower()
        if status and status not in TERMINAL_SUBMISSION_STATUSES:
            self._active_submissions_by_index[index] = item

    def boot_items(self) -> list[TextualFeedItem]:
        """Itens do cabeçalho de boot: logo, linhas, blocos de status e a régua.

        A régua (``boot_separator``) é sempre o último item do cabeçalho.
        """
        return [item for item in self._items if item.event.boot]

    def clear_chat(self) -> None:
        """Limpa o chat e mantém o cabeçalho de boot.

        O cabeçalho (eventos com ``boot=True``: logo, projeto, bloco MCP,
        caminhos de log) é a parte estática do feed; tudo o que veio do chat
        — histórico restaurado, turnos, transitórios, previews de tools — sai.
        Os blocos de status continuam atualizáveis no lugar depois da limpeza.
        ``_finalized_agents`` fica: um evento tardio de agente já encerrado
        continua ignorado até a próxima run, em vez de virar transitório
        fantasma no chat recém-limpo.
        """
        self._items[:] = self.boot_items()
        self._reset_live_state()
        self._reindex_transients()

    def _reset_live_state(self) -> None:
        """Zera o estado por agente que só existe por causa de itens do chat."""
        self._transient_index_by_agent.clear()
        self._active_submissions_by_index.clear()
        self._kept_answer_by_agent.clear()
        self._transient_tools_by_agent.clear()
        self._tool_call_line_by_agent.clear()
        self._tool_request_lines_by_agent.clear()
        self._tool_request_seen_at_by_agent.clear()
        self._tool_request_expiry_by_agent.clear()
        self._completed_tool_requests_by_agent.clear()
        self._mcp_http_tool_stats_by_agent.clear()
        self._pending_turn_summary_by_agent.clear()
        self._delegation_slots.clear()
        self._delegation_child_slot.clear()

    def hydrate_from_history(
            self,
            messages: list,
            *,
            user_label: str = ">>>",
            agent_resolver: Callable[[str], tuple[str, str] | None] | None = None,
            theme: str | None = None,
    ) -> bool:
        """Reconstrói itens persistentes do feed a partir do histórico salvo."""
        theme_name = str(theme or "").strip()
        hydrated: list[TextualFeedItem] = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip()
            content = strip_ansi(str(message.get("content") or "")).strip("\r\n")
            if not role or not content.strip():
                continue
            if role == USER_ROLE:
                payload = {
                    "content": content,
                    "label": user_label,
                    "style": "green",
                    "render_mode": "plain",
                }
                if theme_name:
                    payload["theme"] = theme_name
                hydrated.append(
                    TextualFeedItem(
                        TextualUiEvent("user_message", payload),
                        transient=False,
                    )
                )
                continue
            style = "cyan"
            label = role
            if callable(agent_resolver):
                try:
                    resolved = agent_resolver(role)
                except Exception as exc:
                    _logger.debug("hydrate_from_history: resolver do agente %s falhou: %s", role, exc)
                    resolved = None
                if resolved:
                    style, label = str(resolved[0] or "cyan"), str(resolved[1] or role)
            payload = {
                "content": content,
                "label": label,
                "style": style,
                "render_mode": "auto",
            }
            if theme_name:
                payload["theme"] = theme_name
            hydrated.append(
                TextualFeedItem(
                    TextualUiEvent("agent_message", payload, agent=role),
                    transient=False,
                )
            )
        if not hydrated:
            return False
        self._items.extend(hydrated)
        return True

    def apply(self, event: TextualUiEvent) -> bool:
        """Aplica evento e retorna se o feed visual precisa ser redesenhado."""
        if event.kind in self._IGNORED_KINDS:
            return False
        if event.kind in {"question", "question_clear"}:
            return False
        if event.kind == "submission_status":
            return self._apply_submission_status(event)
        if event.kind == "boot_status":
            return self._apply_boot_status(event)
        if event.boot:
            return self._append_header_item(TextualFeedItem(event, transient=False))
        if event.kind == "visual_reset":
            return self._apply_visual_reset(event)
        if event.kind == "agent_message":
            delegation_id = self._event_delegation_id(event)
            if delegation_id and delegation_id in self._delegation_slots:
                return self._finish_delegation_group(event, delegation_id)
            self._drop_kept_answer_repeated_by(event)
            final_key = self._final_replacement_key(event)
            summary = self._pop_pending_turn_summary(event, key=final_key)
            burst_stats, _, absorbed_tools = self._consume_mcp_tool_bursts(event, exclude_key=final_key)
            footer = self._turn_summary_footer(summary, burst_stats)
            final_event = event
            if footer is not None:
                payload = (
                    dict(event.payload)
                    if isinstance(event.payload, dict)
                    else {"content": str(event.payload or "")}
                )
                payload["turn_summary"] = footer
                final_event = TextualUiEvent(event.kind, payload, agent=event.agent)
            self._replace_transient_with_final(final_event, final_key, absorbed_tools=absorbed_tools)
            return True
        if event.kind == "turn_summary":
            # Os stats do turno só existem como rodapé da resposta final, que
            # chega em seguida. A pendência é pela chave da execução: outra
            # execução simultânea do mesmo agente tem resumo próprio. Resumo
            # ambíguo (sem run e com várias execuções vivas) fica sem dono.
            key = self._resolve_run_key(event) or self._agent_base(event)
            self._pending_turn_summary_by_agent[key] = event
            return False
        if event.kind in self._TRANSIENT_KINDS or event.kind == "tool_preview":
            bound = self._bind_to_run(event)
            if bound is None:
                return False
            event = bound
        if self._is_new_run_signal(event):
            # Um resumo cuja resposta final nunca entrou no feed (delegação
            # oculta, tentativa descartada) não pode virar rodapé do próximo
            # turno do mesmo agente.
            self._pop_pending_turn_summary(event)
        if event.kind == "tool_preview":
            return self._apply_tool_preview(event)
        if event.kind == "tool_state":
            return self._apply_tool_state(event)
        if event.kind == "delegation":
            return self._apply_delegation(event)
        if event.kind in self._TRANSIENT_KINDS:
            agent = self._agent_key(event)
            if self._is_finalized_agent(agent):
                # agent_update e lifecycle RUNNING sinalizam a nova run e evitam
                # que ela fique invisível.
                if self._is_new_run_signal(event):
                    self._unmark_finalized(agent)
                    self._drop_tool_lines(agent)
                else:
                    return False
            if self._is_run_boundary_lifecycle(event):
                self._drop_tool_lines(agent)
                self._kept_answer_by_agent.pop(agent, None)
                if self._is_final_lifecycle(event):
                    self._mark_finalized(agent)
                    return self._close_delegation_group(event, self._event_delegation_id(event), agent)
            if event.kind == "agent_update" and self._is_answer_event(event):
                trimmed = self._without_kept_answer(agent, event)
                if trimmed is None:
                    return False
                event = trimmed
            if event.kind == "pending_input":
                self._keep_partial_answer(agent)
            self._upsert_transient(event)
            return True
        item = TextualFeedItem(event, transient=False)
        self._items.append(item)
        if event.kind == "user_message":
            self._track_submission_item(len(self._items) - 1, item)
        return True

    @staticmethod
    def _boot_status_key(event: TextualUiEvent) -> str:
        payload = event.payload if isinstance(event.payload, dict) else {}
        return str(payload.get("key") or "")

    def _apply_boot_status(self, event: TextualUiEvent) -> bool:
        """Substitui no lugar o bloco de status com a mesma chave, ou o anexa.

        O bloco vive no cabeçalho de boot e continua atualizável depois que o
        histórico restaurado e as mensagens do chat foram anexados abaixo.
        """
        key = self._boot_status_key(event)
        item = TextualFeedItem(event, transient=False)
        for index, existing in enumerate(self._items):
            if existing.event.kind != "boot_status":
                continue
            if self._boot_status_key(existing.event) != key:
                continue
            if existing.event.payload == event.payload:
                return False
            self._items[index] = item
            return True
        if event.boot:
            return self._append_header_item(item)
        self._items.append(item)
        return True

    def _boot_separator_index(self) -> int | None:
        for index, existing in enumerate(self._items):
            if existing.event.kind == "boot_separator":
                return index
        return None

    def _append_header_item(self, item: TextualFeedItem) -> bool:
        """Anexa ``item`` ao fim do cabeçalho de boot, acima da régua.

        A régua (``boot_separator``) é criada pelo próprio modelo junto com o
        primeiro item do cabeçalho e fica sempre como último item de boot: é
        ela que separa o cabeçalho do chat, qualquer que seja a ordem em que
        logo, linhas de boot e blocos de status chegam. Um item de boot que
        chega depois de o chat ter começado entra no cabeçalho, não no meio
        do chat; os índices dos transitórios abaixo são recalculados.
        """
        separator_index = self._boot_separator_index()
        position = 0 if separator_index is None else separator_index
        self._items.insert(position, item)
        header_end = position + 1
        if separator_index is None:
            separator = TextualUiEvent("boot_separator", compact=True, boot=True)
            self._items.insert(header_end, TextualFeedItem(separator, transient=False))
            header_end += 1
        if header_end < len(self._items):
            self._reindex_transients()
        elif item.event.kind == "user_message":
            self._track_submission_item(position, item)
        return True

    def _apply_submission_status(self, event: TextualUiEvent) -> bool:
        """Substitui o status dentro do turno humano correlacionado."""
        status_payload = dict(event.payload or {}) if isinstance(event.payload, dict) else {}
        submission_id = str(status_payload.get("submission_id") or "")
        if not submission_id:
            return False
        for index in range(len(self._items) - 1, -1, -1):
            item = self._items[index]
            if item.event.kind != "user_message" or not isinstance(item.event.payload, dict):
                continue
            payload = dict(item.event.payload)
            if str(payload.get("submission_id") or "") != submission_id:
                continue
            current_status = payload.get("submission")
            current_revision = (
                int(current_status.get("revision", -1))
                if isinstance(current_status, dict)
                else -1
            )
            incoming_revision = int(
                status_payload.get("revision", current_revision + 1)
            )
            if incoming_revision <= current_revision:
                return False
            payload["submission"] = status_payload
            replacement = TextualFeedItem(
                TextualUiEvent(
                    "user_message",
                    payload,
                    agent=item.event.agent,
                ),
                transient=False,
            )
            self._items[index] = replacement
            self._track_submission_item(index, replacement)
            return True
        return False

    @staticmethod
    def _event_delegation_id(event: TextualUiEvent) -> str:
        """Extrai o identificador de delegação declarado no payload do evento."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        return str(payload.get("delegation_id") or "").strip()

    @staticmethod
    def _event_run_id(event: TextualUiEvent) -> str:
        """Extrai o run_id declarado no payload do evento."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        return str(payload.get("run_id") or "").strip()

    @staticmethod
    def _delegation_occurrence(event: TextualUiEvent) -> int:
        """Ocorrência do cartão entre os cartões vivos com o mesmo delegation_id."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        try:
            return max(1, int(payload.get("delegation_occurrence") or 1))
        except (TypeError, ValueError):
            return 1

    def _item_event_for_key(self, key: str) -> TextualUiEvent | None:
        index = self._transient_index_by_agent.get(key)
        if index is None or not (0 <= index < len(self._items)):
            return None
        return self._items[index].event

    def _apply_delegation(self, event: TextualUiEvent) -> bool:
        """Exibe a delegação como bloco transitório do delegate feed.

        A delegação é artefato de execução, não mensagem de chat: fica visível
        somente enquanto a execução correlacionada está ativa e é removida
        junto com o feed do agente delegado ao término.
        """
        self._consume_delegate_tool_preview(event)
        delegation_id = self._event_delegation_id(event)
        if not delegation_id:
            # Sem correlação não há como remover o cartão ao término.
            self._items.append(TextualFeedItem(event, transient=False))
            return True
        slots = self._delegation_slots.setdefault(delegation_id, [])
        paired = set(self._delegation_child_slot.values())
        unpaired = [slot for slot in slots if slot not in paired]
        if unpaired:
            # Cartão ainda sem execução delegada é indistinguível do novo:
            # é a mesma delegação reexibida, não uma nova ocorrência.
            occurrence = self._delegation_occurrence(
                self._item_event_for_key(unpaired[-1]) or event
            )
        else:
            occurrence = 1 + max(
                (
                    self._delegation_occurrence(slot_event)
                    for slot in slots
                    if (slot_event := self._item_event_for_key(slot)) is not None
                ),
                default=0,
            )
        if occurrence > 1:
            payload = dict(event.payload) if isinstance(event.payload, dict) else {}
            payload["delegation_occurrence"] = occurrence
            event = TextualUiEvent(event.kind, payload, agent=event.agent)
        slot_key = self._agent_key(event)
        if slot_key not in slots:
            slots.append(slot_key)
        self._upsert_transient(event)
        return True

    def _delegation_transient_keys(
        self,
        delegation_id: str,
        *,
        own_slots: list[str],
        run_id: str = "",
    ) -> list[str]:
        """Chaves transitórias da delegação que pertencem aos cartões ``own_slots``.

        Com o mesmo delegation_id vivo mais de uma vez, os outros cartões e as
        execuções pareadas a eles ficam; uma execução com outro run_id também.
        """
        if not delegation_id:
            return []
        all_slots = set(self._delegation_slots.get(delegation_id, ()))
        foreign_children = {
            child
            for child, slot in self._delegation_child_slot.items()
            if slot in all_slots and slot not in own_slots
        }
        keys: list[str] = []
        for key, index in self._transient_index_by_agent.items():
            if key in all_slots or key in foreign_children:
                continue
            if not (0 <= index < len(self._items)):
                continue
            item_event = self._items[index].event
            if self._event_delegation_id(item_event) != delegation_id:
                continue
            item_run_id = self._event_run_id(item_event)
            if run_id and item_run_id and item_run_id != run_id:
                continue
            keys.append(key)
        return keys

    def _own_delegation_slots(self, slots: list[str], keys: list[str]) -> list[str]:
        """Cartões que morrem com ``keys``: o pareado ou o mais antigo sem execução."""
        own = [
            slot
            for key in keys
            if (slot := self._delegation_child_slot.get(key)) is not None and slot in slots
        ]
        own.extend(slot for slot in slots if slot in keys and slot not in own)
        if own:
            return own
        # Chave de fechamento sem execução pareada (delegation_id sem run_id,
        # como em clear_agent_transient): o cartão é o mais antigo pareado a
        # uma execução do mesmo agente. Só então vale o mais antigo sem
        # execução.
        bases = {self._key_base(key) for key in keys}
        same_agent = [
            slot
            for slot in slots
            if any(
                paired_slot == slot and self._key_base(child) in bases
                for child, paired_slot in self._delegation_child_slot.items()
            )
        ]
        if same_agent:
            return same_agent[:1]
        paired = set(self._delegation_child_slot.values())
        return [slot for slot in slots if slot not in paired][:1]

    def _delegation_group_keys(
        self,
        delegation_id: str,
        *,
        agent_keys: list[str] | None = None,
        run_id: str = "",
    ) -> list[str]:
        """Reúne as chaves transitórias que morrem junto com a delegação."""
        keys = list(agent_keys or [])
        identifiers = {delegation_id} if delegation_id else set()
        for key in list(keys):
            item_event = self._item_event_for_key(key)
            if item_event is None:
                continue
            item_id = self._event_delegation_id(item_event)
            if item_id:
                identifiers.add(item_id)
        for identifier in identifiers:
            slots = self._delegation_slots.get(identifier)
            if not slots:
                continue
            own_slots = self._own_delegation_slots(slots, keys)
            keys.extend(own_slots)
            keys.extend(
                self._delegation_transient_keys(identifier, own_slots=own_slots, run_id=run_id)
            )
        ordered: list[str] = []
        for key in keys:
            if key not in ordered:
                ordered.append(key)
        return ordered

    def _finish_delegation_group(self, event: TextualUiEvent, delegation_id: str) -> bool:
        """Encerra a delegação removendo o feed da execução delegada e o seu cartão."""
        agent_key = self._agent_key(event)
        self._mark_finalized(agent_key)
        self._kept_answer_by_agent.pop(agent_key, None)
        self._pop_pending_turn_summary(event)
        return self._close_delegation_group(event, delegation_id, agent_key)

    def _close_delegation_group(self, event: TextualUiEvent, delegation_id: str, agent_key: str) -> bool:
        """Remove cartão e execuções que morrem com a delegação encerrada por ``event``.

        As execuções do mesmo agente que caem junto ficam finalizadas: o evento
        de fechamento pode vir sem run_id (chave ``agente#delegação``) e um
        evento atrasado da execução não pode reabrir o cartão.
        """
        keys = self._delegation_group_keys(
            delegation_id,
            agent_keys=[agent_key],
            run_id=self._event_run_id(event),
        )
        base = self._agent_base(event)
        for key in keys:
            if key != agent_key and key in self._transient_index_by_agent and self._key_base(key) == base:
                self._mark_finalized(key)
        return self._remove_transient_keys(keys)

    @staticmethod
    def _key_base(key: str) -> str:
        """Agente de uma chave transitória (``agente#run:…`` → ``agente``)."""
        return key.split("#", 1)[0]

    def _pop_pending_turn_summary(
        self,
        event: TextualUiEvent,
        *,
        key: str | None = None,
    ) -> TextualUiEvent | None:
        """Retira o resumo pendente da execução do evento (ou de ``key``).

        Resumo sem execução (chave base) é de quem chegar primeiro; resumo de
        uma execução nunca vira rodapé de outra.
        """
        pending = self._pending_turn_summary_by_agent
        summary = pending.pop(key or self._agent_key(event), None)
        unowned = pending.pop(self._agent_base(event), None)
        return summary if summary is not None else unowned

    @staticmethod
    def _agent_base(event: TextualUiEvent) -> str:
        """Agente sem run nem delegação: a identidade que liga resumo e resposta final."""
        return str(event.agent or "__global__")

    def _agent_key(self, event: TextualUiEvent) -> str:
        payload = event.payload if isinstance(event.payload, dict) else {}
        run_id = str(payload.get("run_id") or "").strip()
        delegation_id = str(payload.get("delegation_id") or "").strip()
        base = self._agent_base(event)
        transport = str(payload.get("transport") or "").strip()
        if transport == "mcp_http":
            # Sessão e run não são identidade visual estável para MCP HTTP:
            # alguns clientes recriam ambos entre chamadas. O clientInfo do
            # handshake representa o cliente lógico e separa consumidores
            # distintos sem fragmentar as tools do mesmo consumidor.
            client_name = str(payload.get("client_name") or "").strip().lower()
            client_version = str(payload.get("client_version") or "").strip().lower()
            if client_name:
                client_key = f"{client_name}@{client_version}" if client_version else client_name
                return f"{base}#mcp-client:{client_key}"
            return base
        if run_id:
            return f"{base}#run:{run_id}"
        if not delegation_id:
            return base
        # A ocorrência fica no payload do próprio cartão para que a chave
        # sobreviva à reindexação dos transitórios.
        occurrence = self._delegation_occurrence(event)
        suffix = f"#{occurrence}" if occurrence > 1 else ""
        return f"{base}#{delegation_id}{suffix}"

    @staticmethod
    def _has_explicit_identity(event: TextualUiEvent) -> bool:
        """Evento que já diz a qual execução pertence (run, delegação ou cliente MCP)."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        return bool(
            str(payload.get("run_id") or "").strip()
            or str(payload.get("delegation_id") or "").strip()
            or str(payload.get("transport") or "").strip() == "mcp_http"
        )

    def _live_run_keys(self, base: str, *, include_mcp: bool = False) -> list[str]:
        """Execuções vivas ``base#…`` do agente, da mais antiga (menor índice) à mais nova.

        Cartões de delegação não são execuções do agente. O burst MCP HTTP
        só conta para a resposta final, que o absorve; um evento CLI não
        tem como herdar a identidade de cliente MCP.
        """
        prefix = f"{base}#"
        live: list[tuple[int, str]] = []
        for key, index in self._transient_index_by_agent.items():
            if not key.startswith(prefix):
                continue
            if not include_mcp and key.startswith(f"{prefix}mcp-client:"):
                continue
            if not (0 <= index < len(self._items)):
                continue
            if self._items[index].event.kind == "delegation":
                continue
            live.append((index, key))
        return [key for _, key in sorted(live)]

    def _resolve_run_key(self, event: TextualUiEvent, *, include_mcp: bool = False) -> str | None:
        """Chave da execução a que o evento pertence; None quando é ambíguo.

        Evento com run_id/delegation_id usa a própria chave. Sem id: o
        transitório ``base`` (de uma execução sem id) é dele; senão a única
        execução viva do agente; sem nenhuma viva, ``base``. Com duas ou
        mais vivas não há como saber a qual se refere.
        """
        if self._has_explicit_identity(event):
            return self._agent_key(event)
        base = self._agent_base(event)
        if base in self._transient_index_by_agent:
            return base
        live = self._live_run_keys(base, include_mcp=include_mcp)
        if not live:
            return base
        if len(live) == 1:
            return live[0]
        return None

    def _bind_to_run(self, event: TextualUiEvent) -> TextualUiEvent | None:
        """Evento com a identidade da execução resolvida, ou None se descartado.

        A identidade inferida é gravada no payload (run_id/delegation_id do
        transitório vivo) porque a chave dos transitórios é recalculada a
        partir do evento em cada reindexação.
        """
        key = self._resolve_run_key(event)
        if key is None:
            if event.kind == "pending_input":
                # Pergunta descartada deixaria o humano sem ver o que responde;
                # sem dono certo ela ganha cartão próprio.
                return event
            _logger.debug(
                "evento sem run_id ambíguo descartado: kind=%s agent=%s vivos=%s",
                event.kind,
                event.agent,
                self._live_run_keys(self._agent_base(event)),
            )
            return None
        if key == self._agent_key(event):
            return event
        live_event = self._item_event_for_key(key)
        live_payload = live_event.payload if live_event is not None and isinstance(live_event.payload, dict) else {}
        payload = dict(event.payload) if isinstance(event.payload, dict) else {"content": str(event.payload or "")}
        for field in ("run_id", "delegation_id"):
            if live_payload.get(field):
                payload[field] = live_payload[field]
        return TextualUiEvent(event.kind, payload, agent=event.agent, compact=event.compact)

    def _upsert_transient(self, event: TextualUiEvent) -> bool:
        agent = self._agent_key(event)
        item = TextualFeedItem(self._with_transient_tools(event), transient=True)
        index = self._transient_index_by_agent.get(agent)
        if index is not None and 0 <= index < len(self._items):
            self._items[index] = item
            return True
        if event.kind != "delegation":
            self._pair_delegation_child(agent, event)
        position = self._delegation_insert_position(agent, event)
        if position is None:
            self._transient_index_by_agent[agent] = len(self._items)
            self._items.append(item)
            return False
        self._items.insert(position, item)
        self._reindex_transients()
        return True

    def _pair_delegation_child(self, child_key: str, event: TextualUiEvent) -> None:
        """Pareia a execução delegada com o cartão mais antigo ainda sem execução."""
        delegation_id = self._event_delegation_id(event)
        if not delegation_id or child_key in self._delegation_child_slot:
            return
        paired = set(self._delegation_child_slot.values())
        for slot in self._delegation_slots.get(delegation_id, ()):
            if slot not in paired:
                self._delegation_child_slot[child_key] = slot
                return

    def _delegation_insert_position(self, child_key: str, event: TextualUiEvent) -> int | None:
        """Posição logo abaixo da delegação correspondente, se houver.

        Mantém o feed do agente delegado ancorado ao cartão da sua delegação
        mesmo quando outras execuções estão ativas no fim do scrollback.
        """
        delegation_id = self._event_delegation_id(event)
        if not delegation_id:
            return None
        slots = self._delegation_slots.get(delegation_id) or []
        slot_key = self._delegation_child_slot.get(child_key) or (slots[0] if slots else None)
        if not slot_key:
            return None
        index = self._transient_index_by_agent.get(slot_key)
        if index is None or not (0 <= index < len(self._items)):
            return None
        position = index + 1
        while position < len(self._items):
            candidate = self._items[position]
            if not candidate.transient:
                break
            if self._event_delegation_id(candidate.event) != delegation_id:
                break
            position += 1
        return position if position < len(self._items) else None

    def _final_tool_lines(self, key: str, *, absorbed: list[str] | None = None) -> list[str]:
        """Linhas de tools da execução guardadas na resposta final, com teto.

        ``absorbed`` são as linhas dos bursts MCP HTTP que a final absorveu;
        entram antes das próprias. O excedente vira a primeira linha, como no
        bloco vivo: "anteriores" fica acima das que sobraram.
        """
        lines = [*(absorbed or ()), *(self._transient_tools_by_agent.get(key) or ())]
        limit = self._MAX_FINAL_TOOL_LINES
        if len(lines) <= limit:
            return lines
        hidden = len(lines) - (limit - 1)
        return [f"⋮ +{hidden} ferramentas anteriores", *lines[-(limit - 1):]]

    def _replace_transient_with_final(
        self,
        event: TextualUiEvent,
        agent: str,
        *,
        absorbed_tools: list[str] | None = None,
    ) -> bool:
        """Troca o transitório da execução ``agent`` pela resposta final."""
        # As linhas de tools são capturadas antes do descarte: a resposta
        # final as guarda para o toggle de detalhes do turno.
        tool_lines = self._final_tool_lines(agent, absorbed=absorbed_tools)
        self._kept_answer_by_agent.pop(agent, None)
        self._drop_tool_lines(agent)
        self._mark_finalized(agent)
        item = TextualFeedItem(event, transient=False)
        if tool_lines:
            # Sem tools não há o que alternar: a final fica com o evento
            # original e sem as chaves do toggle.
            payload = dict(event.payload) if isinstance(event.payload, dict) else {"content": str(event.payload or "")}
            payload["tool_lines"] = tool_lines
            payload["tools_expanded"] = False
            payload["feed_seq"] = item.seq
            item.event = TextualUiEvent(event.kind, payload, agent=event.agent, compact=event.compact)
        index = self._transient_index_by_agent.pop(agent, None)
        if index is not None and 0 <= index < len(self._items):
            self._items[index] = item
            return True
        self._items.append(item)
        return False

    def _final_replacement_key(self, event: TextualUiEvent) -> str:
        """Execução que a resposta final substitui.

        Sem run_id e com várias execuções vivas, a final fecha a mais antiga:
        nunca abre um cartão extra ao lado das vivas.
        """
        key = self._resolve_run_key(event, include_mcp=True)
        if key is not None:
            return key
        live = self._live_run_keys(self._agent_base(event), include_mcp=True)
        _logger.debug(
            "evento sem run_id ambíguo: resposta final de %s substitui a execução mais antiga %s (vivas=%s)",
            event.agent,
            live[0],
            live,
        )
        return live[0]

    def toggle_turn_tools(self, seq: int) -> int | None:
        """Alterna a exibição das tools da resposta final ``seq``; devolve o índice.

        O item é substituído preservando o seq, que é a identidade usada pela
        UI para o clique. None quando o item não existe ou não tem tools.
        """
        for index in range(len(self._items) - 1, -1, -1):
            item = self._items[index]
            if item.seq != seq:
                continue
            payload = item.event.payload
            if not isinstance(payload, dict) or not payload.get("tool_lines"):
                return None
            toggled = {**payload, "tools_expanded": not payload.get("tools_expanded")}
            event = item.event
            self._items[index] = TextualFeedItem(
                TextualUiEvent(event.kind, toggled, agent=event.agent, compact=event.compact, boot=event.boot),
                transient=False,
                seq=seq,
            )
            return index
        return None

    def latest_toggleable_seq(self) -> int | None:
        """Seq da resposta final mais recente que tem tools para alternar."""
        for item in reversed(self._items):
            payload = item.event.payload
            if not item.transient and isinstance(payload, dict) and payload.get("tool_lines"):
                return item.seq
        return None

    def _is_finalized_agent(self, agent: str) -> bool:
        return agent in self._finalized_agents

    def _mark_finalized(self, agent: str) -> None:
        self._finalized_agents[agent] = None
        self._finalized_agents.move_to_end(agent)
        while len(self._finalized_agents) > self._MAX_FINALIZED_AGENTS:
            self._finalized_agents.popitem(last=False)

    def _unmark_finalized(self, agent: str) -> None:
        self._finalized_agents.pop(agent, None)

    def _drop_tool_lines(self, agent: str) -> None:
        """Descarta as previews de tools da execução e o mapa por tool_call_id."""
        self._transient_tools_by_agent.pop(agent, None)
        self._tool_call_line_by_agent.pop(agent, None)

    @staticmethod
    def _is_final_lifecycle(event: TextualUiEvent) -> bool:
        if event.kind != "agent_lifecycle":
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        status = _coerce_lifecycle_status(payload.get("status"))
        # Re-tentativa e reconexão chegam como RETRYING/RECONNECTING; o texto
        # da mensagem não decide se a execução terminou.
        return status in {
            AgentLifecycleStatus.COMPLETED,
            AgentLifecycleStatus.FAILED,
            AgentLifecycleStatus.ERROR,
            AgentLifecycleStatus.CANCELLED,
            AgentLifecycleStatus.ABORTED,
        }

    @staticmethod
    def _is_new_run_signal(event: TextualUiEvent) -> bool:
        """Retorna True para o primeiro sinal visual de uma nova execução do agente."""
        if event.kind == "agent_update":
            return True
        if event.kind != "agent_lifecycle":
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        return _coerce_lifecycle_status(payload.get("status")) is AgentLifecycleStatus.RUNNING

    @staticmethod
    def _is_run_boundary_lifecycle(event: TextualUiEvent) -> bool:
        """Retorna True quando lifecycle encerra a execução transitória anterior."""
        if event.kind != "agent_lifecycle":
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        return _coerce_lifecycle_status(payload.get("status")) in RUN_BOUNDARY_LIFECYCLE_STATUSES

    @staticmethod
    def _is_answer_event(event: TextualUiEvent) -> bool:
        payload = event.payload if isinstance(event.payload, dict) else {}
        return bool(payload.get("answer"))

    def _without_kept_answer(self, agent: str, event: TextualUiEvent) -> TextualUiEvent | None:
        """Tira da resposta parcial o trecho já fixado; None se nada sobrar."""
        kept, _ = self._kept_answer_by_agent.get(agent, ("", []))
        payload = event.payload if isinstance(event.payload, dict) else {}
        content = str(payload.get("content") or "")
        # Se o modelo reescreveu o texto já fixado, a atualização entra inteira.
        if not kept or not content.startswith(kept):
            return event
        remainder = content[len(kept):].strip()
        if not remainder:
            return None
        return TextualUiEvent(
            event.kind,
            {**payload, "content": remainder, "answer_full": content},
            agent=event.agent,
        )

    def _keep_partial_answer(self, agent: str) -> bool:
        """Fixa no feed a resposta parcial do agente antes do cartão de pergunta.

        Sem isso o cartão ``pending_input`` ocupa o lugar do transitório e o
        texto que o agente escreveu para contextualizar a pergunta some.
        """
        index = self._transient_index_by_agent.get(agent)
        if index is None or not (0 <= index < len(self._items)):
            return False
        event = self._items[index].event
        if event.kind != "agent_update" or not self._is_answer_event(event):
            return False
        payload = dict(event.payload)
        content = str(payload.get("content") or "").strip()
        if not content:
            return False
        full_answer = str(payload.pop("answer_full", "") or content).strip()
        payload.pop("answer", None)
        payload.pop("tools", None)
        payload["content"] = content
        kept_item = TextualFeedItem(
            TextualUiEvent("agent_message", payload, agent=event.agent),
            transient=False,
        )
        self._items[index] = kept_item
        self._transient_index_by_agent.pop(agent, None)
        self._drop_tool_lines(agent)
        _, kept_items = self._kept_answer_by_agent.get(agent, ("", []))
        self._kept_answer_by_agent[agent] = (full_answer, [*kept_items, kept_item])
        return True

    def _drop_kept_answer_repeated_by(self, event: TextualUiEvent) -> bool:
        """Remove os trechos fixados quando a resposta final já os repete.

        A final pode chegar sem ``run_id`` enquanto o trecho foi fixado com a
        chave da run; sem chave exata, vale o único trecho fixado do agente.
        """
        key = self._agent_key(event)
        if key not in self._kept_answer_by_agent:
            base = self._agent_base(event)
            candidates = [
                candidate
                for candidate in self._kept_answer_by_agent
                if candidate == base or candidate.startswith(f"{base}#")
            ]
            if len(candidates) != 1:
                return False
            key = candidates[0]
        kept, kept_items = self._kept_answer_by_agent[key]
        if isinstance(event.payload, dict):
            content = str(event.payload.get("content") or "").strip()
        else:
            content = str(event.payload or "").strip()
        if not content.startswith(kept):
            return False
        self._kept_answer_by_agent.pop(key, None)
        before = len(self._items)
        self._items = [item for item in self._items if not any(item is kept_item for kept_item in kept_items)]
        if len(self._items) == before:
            return False
        self._reindex_transients()
        return True

    def _apply_visual_reset(self, event: TextualUiEvent) -> bool:
        """Remove estado visual transitório sem apagar mensagens persistentes."""
        agent = str(event.agent or "").strip()
        if agent:
            payload = event.payload if isinstance(event.payload, dict) else {}
            if payload.get("run_id") or payload.get("delegation_id"):
                keys = [self._agent_key(event)]
            else:
                agent_prefix = f"{agent}#"
                # Execuções do agente que seguem vivas não pertencem a este
                # reset: sem run_id ele não sabe a qual execução se refere.
                kept = {
                    f"{agent}#run:{run_id}"
                    for run_id in payload.get("keep_run_ids") or ()
                    if str(run_id or "").strip()
                }
                keys = [
                    key
                    for key in set(
                        self._transient_index_by_agent
                        | self._transient_tools_by_agent
                    )
                    if (key == agent or key.startswith(agent_prefix)) and key not in kept
                ]
            removed = self._remove_transient_keys(
                self._delegation_group_keys(
                    self._event_delegation_id(event),
                    agent_keys=keys,
                )
            )
            return removed

        before = len(self._items)
        self._items = [item for item in self._items if not item.transient]
        self._transient_index_by_agent.clear()
        self._kept_answer_by_agent.clear()
        self._transient_tools_by_agent.clear()
        self._tool_call_line_by_agent.clear()
        self._tool_request_lines_by_agent.clear()
        self._tool_request_seen_at_by_agent.clear()
        self._tool_request_expiry_by_agent.clear()
        self._completed_tool_requests_by_agent.clear()
        self._mcp_http_tool_stats_by_agent.clear()
        self._delegation_slots.clear()
        self._delegation_child_slot.clear()
        self._reindex_transients()
        return len(self._items) != before

    def _remove_transient_keys(
        self,
        keys: list[str],
        *,
        preserve_mcp_stats: bool = False,
    ) -> bool:
        indexes = sorted(
            {
                index
                for key in keys
                if (index := self._transient_index_by_agent.pop(key, None)) is not None
                and 0 <= index < len(self._items)
            },
            reverse=True,
        )
        for key in keys:
            for delegation_id, slots in list(self._delegation_slots.items()):
                if key in slots:
                    slots.remove(key)
                if not slots:
                    self._delegation_slots.pop(delegation_id, None)
            self._delegation_child_slot.pop(key, None)
            for child, slot_key in list(self._delegation_child_slot.items()):
                if slot_key == key:
                    self._delegation_child_slot.pop(child, None)
            self._drop_tool_lines(key)
            self._tool_request_lines_by_agent.pop(key, None)
            self._tool_request_seen_at_by_agent.pop(key, None)
            self._tool_request_expiry_by_agent.pop(key, None)
            if not preserve_mcp_stats:
                self._completed_tool_requests_by_agent.pop(key, None)
                self._mcp_http_tool_stats_by_agent.pop(key, None)
        if not indexes:
            return False
        for index in indexes:
            del self._items[index]
        self._reindex_transients()
        return True

    def _reindex_transients(self) -> None:
        """Reconstrói índices posicionais apenas quando slots são inseridos/removidos."""
        self._transient_index_by_agent.clear()
        self._active_submissions_by_index.clear()
        for index, item in enumerate(self._items):
            if item.transient:
                self._transient_index_by_agent[self._agent_key(item.event)] = index
            elif item.event.kind == "user_message":
                self._track_submission_item(index, item)

    @staticmethod
    def _tool_preview_subject(content: str) -> str:
        """Deriva a identidade de uma tool a partir da linha de preview.

        Remove o marcador de status inicial ("$", "✓", "✗", "⌘") e a anotação
        final "(exit N)" para que as linhas de início e conclusão de um mesmo
        comando/ferramenta compartilhem o mesmo identificador.
        """
        text = str(content or "").strip()
        if not text:
            return ""
        markers = ("◇ ", "$ ", "✓ ", "✗ ", "⌘ ", "⚒ ")
        changed = True
        while changed:
            changed = False
            for marker in markers:
                if text.startswith(marker):
                    text = text[len(marker):].strip()
                    changed = True
                    break
        text = re.sub(r"\s*\(exit\s+-?\d+\)\s*$", "", text).strip()
        return text

    @classmethod
    def _tool_preview_content(cls, event: TextualUiEvent) -> str:
        payload = event.payload if isinstance(event.payload, dict) else {}
        content_source = payload.get("content") if payload else event.payload
        content = strip_ansi(str(content_source or "")).strip()
        transport = str(payload.get("transport") or "").strip()
        if content and transport == "mcp_http":
            marker = f"{cls._MCP_HTTP_TOOL_MARKER} "
            if not content.startswith(marker):
                content = f"{marker}{content}"
        return content

    @staticmethod
    def _tool_preview_tool_name(subject: str) -> str:
        """Extrai o nome canônico da tool de uma linha de preview.

        Normaliza o prefixo genérico "usando " e o namespace MCP ("quimera_")
        para que a linha genérica emitida pelo CLI e a linha rica emitida pelo
        executor de tools refiram-se à mesma identidade.
        """
        text = str(subject or "").strip()
        if text.startswith("usando "):
            text = text[7:].strip()
        token = text.split(" ", 1)[0].strip().strip(":")
        for prefix in ("mcp__quimera__", "quimera_"):
            if token.startswith(prefix):
                token = token[len(prefix):]
                break
        return token

    def _merge_generic_tool_line(self, lines: list[str], content: str, subject: str) -> bool:
        """Funde a linha genérica "usando X" com a linha rica da mesma tool.

        A mesma chamada chega por dois canais (stdout do CLI e preview do
        executor); manter as duas duplica o feed. Retorna True quando a linha
        nova foi absorvida por uma existente (descartada ou substituindo-a).
        """
        tool_name = self._tool_preview_tool_name(subject)
        if not tool_name:
            return False
        new_is_generic = str(content).strip().startswith("usando ")
        for idx in range(len(lines) - 1, -1, -1):
            existing_subject = self._tool_preview_subject(lines[idx])
            if self._tool_preview_tool_name(existing_subject) != tool_name:
                continue
            if new_is_generic:
                return True
            if existing_subject.startswith("usando "):
                lines[idx] = content
                return True
            return False
        return False

    def _consume_delegate_tool_preview(self, event: TextualUiEvent) -> bool:
        """Remove a preview da chamada de delegação quando o cartão rico chega.

        O casamento é pelo ``tool_call_id`` exato: dois orquestradores podem
        delegar a mesma task ao mesmo tempo, e o texto da preview não diz de
        quem é o cartão. Delegação sem tool_call_id não consome nada.
        """
        payload = event.payload if isinstance(event.payload, dict) else {}
        tool_call_id = str(payload.get("tool_call_id") or "").strip()
        if not tool_call_id:
            _logger.debug(
                "delegação sem tool_call_id: preview de tasks_delegate mantida (delegation_id=%s)",
                payload.get("delegation_id"),
            )
            return False
        match = self._find_tool_call_line(
            tool_call_id,
            from_agent=str(payload.get("from_agent") or "").strip(),
            source_run_id=str(payload.get("source_run_id") or "").strip(),
        )
        if match is None:
            # Passos seguintes da mesma chamada já encontram a preview consumida.
            return False
        agent, line = match
        self._tool_call_line_by_agent[agent].pop(tool_call_id, None)
        lines = self._transient_tools_by_agent[agent]
        lines.remove(line)
        if not lines:
            self._transient_tools_by_agent.pop(agent, None)
        item_index = self._transient_index_by_agent.get(agent, -1)
        if 0 <= item_index < len(self._items):
            current_item = self._items[item_index]
            current_payload = current_item.event.payload
            if isinstance(current_payload, dict):
                clean_payload = dict(current_payload)
                clean_payload.pop("tools", None)
            else:
                clean_payload = current_payload
            clean_event = TextualUiEvent(
                current_item.event.kind,
                clean_payload,
                agent=current_item.event.agent,
            )
            self._items[item_index] = TextualFeedItem(
                self._with_transient_tools(clean_event),
                transient=True,
            )
        return True

    def _find_tool_call_line(
        self,
        tool_call_id: str,
        *,
        from_agent: str,
        source_run_id: str = "",
    ) -> tuple[str, str] | None:
        """Execução e linha visível da preview da chamada ``tool_call_id``.

        Procura na execução exata (``from_agent#run:source_run_id``) e depois
        nas demais execuções do mesmo agente, sem distinguir caixa: o cartão
        traz o nome normalizado pela tool e a preview o nome do perfil. Só
        quando o orquestrador não tem execução nenhuma no feed (cliente HTTP
        anônimo: "agente" no cartão, "mcp-http" na preview) vale qualquer
        execução, porque o tool_call_id já é único por execução.
        """
        base = from_agent.casefold()
        exact = f"{base}#run:{source_run_id}" if base and source_run_id else None
        exact_keys: list[str] = []
        same_agent: list[str] = []
        others: list[str] = []
        for agent in self._tool_call_line_by_agent:
            folded = agent.casefold()
            if exact is not None and folded == exact:
                exact_keys.append(agent)
            elif base and self._key_base(folded) == base:
                same_agent.append(agent)
            else:
                others.append(agent)
        candidates = [*exact_keys, *same_agent]
        # Sem execução do orquestrador no feed, o id vale sozinho, mas só para
        # uma preview de tasks_delegate: ids sequenciais de dois perfis
        # OpenAI-compat podem coincidir e não se tira uma tool qualquer de
        # outro agente.
        guard_tool_name = not candidates
        for agent in candidates or others:
            line = self._tool_call_line_by_agent[agent].get(tool_call_id)
            if line is None or line not in self._transient_tools_by_agent.get(agent, ()):
                continue
            if guard_tool_name and self._tool_preview_tool_name(self._tool_preview_subject(line)) != "tasks_delegate":
                continue
            return agent, line
        return None

    def _with_transient_tools(self, event: TextualUiEvent) -> TextualUiEvent:
        """Anexa previews de tools ao estado transitório do agente."""
        agent = self._agent_key(event)
        tool_lines = self._transient_tools_by_agent.get(agent)
        if not tool_lines:
            return event
        payload = event.payload
        if isinstance(payload, dict):
            merged = dict(payload)
        else:
            merged = {"content": str(payload or "")}
        if self._is_mcp_http_payload(merged):
            for key in (
                "tool_total",
                "tool_ok_count",
                "tool_err_count",
                "tool_duration_ms",
            ):
                merged.pop(key, None)
            stats = self._mcp_http_tool_stats_by_agent.get(agent)
            if stats:
                merged.update(
                    {
                        "tool_total": stats["total"],
                        "tool_ok_count": stats["ok_count"],
                        "tool_err_count": stats["err_count"],
                        "tool_duration_ms": stats["duration_ms"],
                    }
                )
        visible_tools = tool_lines[-self._MAX_VISIBLE_TOOL_LINES:]
        hidden_count = len(tool_lines) - len(visible_tools)
        if hidden_count > 0:
            visible_tools = [
                f"⋮ +{hidden_count} ferramentas anteriores",
                *visible_tools,
            ]
        merged["tools"] = visible_tools
        return TextualUiEvent(event.kind, merged, agent=event.agent)

    def _consume_mcp_tool_bursts(
        self,
        event: TextualUiEvent,
        *,
        exclude_key: str,
    ) -> tuple[dict[str, int] | None, bool, list[str]]:
        """Absorve os bursts MCP HTTP do agente na resposta final.

        O burst tem identidade própria (``#mcp-client:``), então a resposta
        final não o substitui; sem esta absorção ele ficaria como bloco
        separado acima da resposta até o dwell expirar, com os stats fora do
        bloco de resultado. Devolve também as linhas de tools dos bursts
        descartados: a final tem chave própria (run) e não as acharia depois.
        """
        base = self._agent_base(event)
        prefix = f"{base}#mcp-client:"
        candidate_keys = set(self._transient_index_by_agent) | set(
            self._mcp_http_tool_stats_by_agent
        )
        matched = [
            key
            for key in candidate_keys
            if (key == base or key.startswith(prefix))
            and (
                key in self._mcp_http_tool_stats_by_agent
                or key in self._tool_request_lines_by_agent
            )
        ]
        if not matched:
            return None, False, []
        totals = {"total": 0, "ok_count": 0, "err_count": 0}
        windows: list[tuple[int, int]] = []
        has_stats = False
        for key in matched:
            stats = self._mcp_http_tool_stats_by_agent.get(key)
            if not stats:
                continue
            has_stats = True
            for field in totals:
                totals[field] += int(stats.get(field) or 0)
            if "started_ms" in stats and "finished_ms" in stats:
                windows.append((stats["started_ms"], stats["finished_ms"]))
        # Bursts do mesmo agente rodam em paralelo: a duração é a janela que
        # cobre todos, não a soma das janelas.
        totals["duration_ms"] = (
            max(end for _, end in windows) - min(start for start, _ in windows)
            if windows
            else 0
        )
        removable = sorted(
            (key for key in matched if key != exclude_key),
            key=lambda key: self._transient_index_by_agent.get(key, len(self._items)),
        )
        absorbed = [
            line
            for key in removable
            for line in self._transient_tools_by_agent.get(key) or ()
        ]
        removed = self._remove_transient_keys(removable) if removable else False
        if exclude_key in matched:
            # O slot excluído vira a própria resposta final; só o estado MCP
            # precisa ser descartado para não vazar para o próximo run.
            self._tool_request_lines_by_agent.pop(exclude_key, None)
            self._tool_request_seen_at_by_agent.pop(exclude_key, None)
            self._tool_request_expiry_by_agent.pop(exclude_key, None)
            self._completed_tool_requests_by_agent.pop(exclude_key, None)
            self._mcp_http_tool_stats_by_agent.pop(exclude_key, None)
        return (totals if has_stats else None), removed, absorbed

    @staticmethod
    def _turn_summary_footer(
        summary: TextualUiEvent | None,
        burst_stats: dict[str, int] | None,
    ) -> dict[str, Any] | None:
        """Payload do rodapé de stats embutido no bloco da resposta final."""
        if summary is not None and isinstance(summary.payload, dict) and summary.payload:
            return dict(summary.payload)
        if not burst_stats or not int(burst_stats.get("total") or 0):
            return None
        duration_ms = int(burst_stats.get("duration_ms") or 0)
        duration = format_duration(duration_ms) if duration_ms > 0 else ""
        return {
            "total": int(burst_stats.get("total") or 0),
            "ok_count": int(burst_stats.get("ok_count") or 0),
            "err_count": int(burst_stats.get("err_count") or 0),
            "duration": duration,
        }

    @staticmethod
    def _is_lifecycle_placeholder_event(event: TextualUiEvent) -> bool:
        """Retorna True para placeholders que não devem virar cabeçalho das tools."""
        if event.kind != "agent_lifecycle":
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        return payload.get("message") == AGENT_EXECUTION_STARTED_MESSAGE

    def _apply_tool_preview(self, event: TextualUiEvent) -> bool:
        """Atualiza previews de tools dentro do bloco transitório do agente."""
        agent = self._agent_key(event)
        if self._is_finalized_agent(agent):
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        content = self._tool_preview_content(event)
        if not content:
            return False
        lines = self._transient_tools_by_agent.setdefault(agent, [])
        is_mcp_http = self._is_mcp_http_payload(payload)
        request_id = self._tool_request_id(payload) if is_mcp_http else ""
        if request_id:
            # MCP HTTP fornece identidade explícita por chamada. Duas requests
            # distintas podem executar exatamente a mesma tool/argumentos e
            # ainda assim precisam ocupar linhas independentes no burst.
            # Portanto só substituímos uma linha quando o request_id é o mesmo;
            # deduplicação textual fica restrita ao caminho CLI sem request_id.
            request_lines = self._tool_request_lines_by_agent.setdefault(agent, {})
            previous_line = request_lines.get(request_id)
            if previous_line and previous_line in lines:
                lines[lines.index(previous_line)] = content
            else:
                lines.append(content)
            request_lines[request_id] = content
            self._tool_request_seen_at_by_agent.setdefault(agent, {}).setdefault(
                request_id,
                self._clock(),
            )
        else:
            # Uma mesma tool CLI costuma emitir uma linha de início ("$ cmd") e
            # outra de conclusão ("✓ cmd"/"✗ cmd (exit N)"). Em vez de acumular
            # as duas, atualizamos a linha existente do mesmo comando no lugar.
            subject = self._tool_preview_subject(content)
            replaced_line = False
            if subject:
                for idx in range(len(lines) - 1, -1, -1):
                    if self._tool_preview_subject(lines[idx]) == subject:
                        lines[idx] = content
                        replaced_line = True
                        break
            if not replaced_line and subject:
                replaced_line = self._merge_generic_tool_line(lines, content, subject)
            if not replaced_line:
                lines.append(content)
        tool_call_id = str(payload.get("tool_call_id") or "").strip()
        if tool_call_id:
            self._tool_call_line_by_agent.setdefault(agent, {})[tool_call_id] = content
        if is_mcp_http:
            self._extend_tool_preview_expiries(agent)
        index = self._transient_index_by_agent.get(agent)
        if index is None or not (0 <= index < len(self._items)):
            transient_payload = dict(payload)
            transient_payload["content"] = ""
            transient_payload["tools"] = list(lines)
            self._upsert_transient(TextualUiEvent("agent_update", transient_payload, agent=event.agent))
            return True
        current_event = self._items[index].event
        if self._is_lifecycle_placeholder_event(current_event):
            current_event = TextualUiEvent("agent_update", {"content": ""}, agent=event.agent)
        self._items[index] = TextualFeedItem(self._with_transient_tools(current_event), transient=True)
        return True

    @staticmethod
    def _tool_request_id(payload: dict[str, Any]) -> str:
        """Identidade estável de uma chamada MCP dentro do agrupamento visual."""
        msg_id = str(payload.get("mcp_msg_id") or payload.get("msg_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        if run_id and msg_id:
            return f"{run_id}#{msg_id}"
        return msg_id or run_id

    @staticmethod
    def _is_mcp_http_payload(payload: dict[str, Any]) -> bool:
        """Retorna se o payload pertence ao transporte MCP HTTP."""
        transport = str(payload.get("transport") or "").strip()
        return transport == "mcp_http"

    @classmethod
    def _terminal_tool_line(cls, preview_line: str, status: str) -> str:
        """Converte a linha running em resultado terminal sem perder argumentos."""
        subject = cls._tool_preview_subject(preview_line)
        marker = "✓" if status == "finished" else "✗"
        has_remote_prefix = str(preview_line).strip().startswith(
            f"{cls._MCP_HTTP_TOOL_MARKER} "
        )
        remote_prefix = f"{cls._MCP_HTTP_TOOL_MARKER} " if has_remote_prefix else ""
        return f"{remote_prefix}{marker} {subject}".strip()

    def _extend_tool_preview_expiries(self, agent: str, *, now: float | None = None) -> None:
        """Mantém o burst MCP HTTP visível enquanto o mesmo cliente segue ativo.

        Agentes CLI limpam previews no fim da execução. MCP HTTP não possui um
        evento equivalente de fim de turno; portanto usamos inatividade do
        cliente como boundary visual e renovamos o prazo de todas as tools
        concluídas quando uma nova chamada chega.
        """
        expiries = self._tool_request_expiry_by_agent.get(agent)
        if not expiries:
            return
        deadline = (self._clock() if now is None else now) + self._tool_preview_dwell_seconds
        for request_id in expiries:
            expiries[request_id] = deadline

    def _apply_tool_state(self, event: TextualUiEvent) -> bool:
        """Finaliza preview MCP HTTP, acumula stats e agenda remoção visual."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        if str(payload.get("transport") or "").strip() != "mcp_http":
            return False

        agent = self._agent_key(event)
        status = str(payload.get("status") or "").strip().lower()
        if status not in {"finished", "failed", "cancelled"}:
            return False

        request_id = self._tool_request_id(payload)
        if request_id:
            completed = self._completed_tool_requests_by_agent.setdefault(agent, set())
            if request_id in completed:
                return False
            completed.add(request_id)
        lines = self._transient_tools_by_agent.get(agent, [])
        request_lines = self._tool_request_lines_by_agent.get(agent, {})
        preview_line = request_lines.get(request_id) if request_id else None
        tool_name = str(payload.get("tool_name") or "").strip()
        if preview_line and preview_line in lines:
            terminal_line = self._terminal_tool_line(preview_line, status)
            lines[lines.index(preview_line)] = terminal_line
            request_lines[request_id] = terminal_line
        elif lines:
            for index in range(len(lines) - 1, -1, -1):
                subject = self._tool_preview_subject(lines[index])
                if self._tool_preview_tool_name(subject) == tool_name:
                    preview_line = lines[index]
                    terminal_line = self._terminal_tool_line(preview_line, status)
                    lines[index] = terminal_line
                    if request_id:
                        request_lines[request_id] = terminal_line
                    break
        if not preview_line:
            preview_line = self._terminal_tool_line(
                f"{self._MCP_HTTP_TOOL_MARKER} ⚒ {tool_name or 'tool'}",
                status,
            )
            lines = self._transient_tools_by_agent.setdefault(agent, [])
            lines.append(preview_line)
            if request_id:
                request_lines = self._tool_request_lines_by_agent.setdefault(agent, {})
                request_lines[request_id] = preview_line

        now = self._clock()
        expiry_id = request_id or f"terminal:{tool_name or 'tool'}"
        self._tool_request_expiry_by_agent.setdefault(agent, {})[expiry_id] = (
            now + self._tool_preview_dwell_seconds
        )
        self._extend_tool_preview_expiries(agent, now=now)

        stats = self._mcp_http_tool_stats_by_agent.setdefault(
            agent,
            {"total": 0, "ok_count": 0, "err_count": 0, "duration_ms": 0},
        )
        stats["total"] += 1
        if status == "finished":
            stats["ok_count"] += 1
        else:
            stats["err_count"] += 1
        # Duração do burst é wall-clock (primeiro início → última conclusão),
        # não a soma: delegações bloqueantes sobrepostas somadas inflavam o
        # rodapé. O início da tool é o mais cedo entre a preview vista e
        # conclusão − duration_ms do servidor, pois a preview pode chegar
        # atrasada ou nem existir.
        finished_ms = round(now * 1000)
        duration_ms = payload.get("duration_ms")
        tool_ms = duration_ms if isinstance(duration_ms, int) and duration_ms >= 0 else 0
        started_ms = finished_ms - tool_ms
        seen_at = self._tool_request_seen_at_by_agent.get(agent, {}).get(request_id) if request_id else None
        if seen_at is not None:
            started_ms = min(started_ms, round(seen_at * 1000))
        stats["started_ms"] = min(stats.get("started_ms", started_ms), started_ms)
        stats["finished_ms"] = max(stats.get("finished_ms", finished_ms), finished_ms)
        stats["duration_ms"] = stats["finished_ms"] - stats["started_ms"]

        transient_index = self._transient_index_by_agent.get(agent)
        if transient_index is not None and 0 <= transient_index < len(self._items):
            current = self._items[transient_index].event
            self._items[transient_index] = TextualFeedItem(
                self._with_transient_tools(current),
                transient=True,
            )
            return True

        transient_payload = dict(payload)
        transient_payload["content"] = ""
        self._upsert_transient(
            TextualUiEvent("agent_update", transient_payload, agent=event.agent)
        )
        return True

    def expire_tool_previews(self) -> bool:
        """Encerra bursts MCP HTTP inativos como uma resposta final de agente CLI.

        Enquanto houver qualquer tool do cliente ainda rodando, o bloco inteiro
        permanece visível. Quando todas estiverem concluídas e o último prazo de
        inatividade vencer, removemos o transient visual. O agregado de stats
        permanece até a resposta final do agente para não depender do dwell.
        """
        now = self._clock()
        expired_agents: list[str] = []
        for agent, expiries in list(self._tool_request_expiry_by_agent.items()):
            request_lines = self._tool_request_lines_by_agent.get(agent, {})
            running_request_ids = set(request_lines) - set(expiries)
            if running_request_ids:
                continue
            if not expiries or any(deadline > now for deadline in expiries.values()):
                continue
            expired_agents.append(agent)

        changed = False
        for agent in expired_agents:
            changed = self._remove_transient_keys(
                [agent],
                preserve_mcp_stats=True,
            ) or changed
        return changed
