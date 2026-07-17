"""Renderer compatível com o contrato legado sobre eventos Textual."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import quimera.themes as themes
from quimera.ui.text import (
    _apply_stream_diff,
    _extract_text_from_renderable,
    _normalize_stream_diff,
    strip_ansi,
)
from quimera.ui.base import RendererBase
from quimera.ui.textual.bridge import TextualUiBridge, _TextualConsoleShim
from quimera.ui.textual.constants import (
    APPROVAL_OPTIONS as _APPROVAL_OPTIONS,
    FAILOVER_DEFAULT_MESSAGE as _FAILOVER_DEFAULT_MESSAGE,
    NO_RESPONSE_MESSAGE as _NO_RESPONSE_MESSAGE,
    RETRY_REASON_LABELS as _RETRY_REASON_LABELS,
)
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import (
    AgentLifecycleStatus,
    _agent_lifecycle_payload,
)
from quimera.ui.textual.terminal_modes import _external_textual_window


class _TextualStatus:
    """Context manager simples para contratos running_status/live_status no Textual."""

    def __init__(self, renderer: "TextualRenderer", agent: str | None = None, initial: str = "") -> None:
        self._renderer = renderer
        self._agent = agent
        self._initial = initial

    def update(self, text: str) -> None:
        self._renderer.update_status(self._agent, text)

    def __enter__(self):
        if self._initial:
            self.update(self._initial)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._renderer.update_status(self._agent, "concluído", status=AgentLifecycleStatus.COMPLETED)
        else:
            self._renderer.update_status(self._agent, "falhou", status=AgentLifecycleStatus.FAILED)


def _resolve_textual_feed_limit(quimera_app) -> int | None:
    """Retorna o limite visual do feed Textual.

    O feed é scrollback visual, não janela de contexto. Configurações como
    history_window e auto_summarize_threshold limitam memória/prompt, mas não
    podem truncar a saída rolável dos agentes.
    """
    return None


def _append_post_exit_failure_message(
    messages: list[tuple[str, str]],
    event: "TextualUiEvent",
) -> bool:
    """Guarda falhas exibidas no alt-screen para reimpressão após a saída."""
    if event.kind not in {"error", "warning"}:
        return False
    content = str(event.payload or "").strip()
    if not content:
        return False
    messages.append((event.kind, content))
    return True


class TextualRenderer(RendererBase):
    """Renderer compatível com a API usada pelo Quimera, emitindo para Textual."""

    supports_agent_feed = True
    supports_structured_agent_activity = True

    def __init__(self, bridge: TextualUiBridge) -> None:
        self._bridge = bridge
        self._audit_logger = None
        self._console = _TextualConsoleShim(bridge)
        self._profile_resolver: Callable | None = None
        self._theme = themes.get(themes.DEFAULT_THEME)
        self._statuses: dict[str, str] = {}
        self._stream_content_by_agent: dict[str, str] = {}
        self._orchestrator_agent: str | None = None

    @property
    def theme_name(self) -> str:
        """Retorna o nome do tema ativo."""
        return self._theme.name

    def cycle_theme(self) -> str:
        """Avança para o próximo tema compartilhado com o renderer legado."""
        all_names = themes.names()
        try:
            idx = all_names.index(self._theme.name)
        except ValueError:
            idx = 0
        next_name = all_names[(idx + 1) % len(all_names)]
        self._theme = themes.get(next_name)
        self._bridge.emit(TextualUiEvent("theme_changed", {"theme": next_name}))
        return next_name

    def set_theme(self, name: str) -> str:
        """Ativa o tema pelo nome e notifica a UI."""
        self._theme = themes.get(name)
        self._bridge.emit(TextualUiEvent("theme_changed", {"theme": self._theme.name}))
        return self._theme.name

    def set_profile_resolver(self, resolver: Callable) -> None:
        """Define callback para resolver (color, label) por agente."""
        self._profile_resolver = resolver

    def set_orchestrator(self, agent_name: str | None) -> None:
        """Define qual agente é o orquestrador ativo."""
        self._orchestrator_agent = str(agent_name).lower().strip() if agent_name else None

    def _resolve_agent_label(self, agent: str) -> str:
        """Retorna label formatada com ícone do agente, ex: '🔮  Claude'."""
        _style, label = self._resolve_agent_style(agent)
        return label

    def _resolve_agent_style(self, agent: str) -> tuple[str, str]:
        """Retorna (style, label) para o agente usando o resolver existente."""
        resolver = self._profile_resolver
        if resolver:
            try:
                result = resolver(str(agent).lower())
                if result:
                    style, label = result
                    return str(style or "cyan"), str(label)
            except Exception:
                pass
        agent_name = str(agent).capitalize() if agent else "Agente"
        return "cyan", f"🤖  {agent_name}"

    def _agent_event_payload(
        self,
        agent,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Monta payload visual comum para eventos de agente."""
        style, label = self._resolve_agent_style(str(agent or ""))
        payload = {"label": label, "style": style, "theme": self._theme.name}
        if self._orchestrator_agent and str(agent or "").lower().strip() == self._orchestrator_agent:
            payload["orchestrator"] = True
        if extra:
            payload.update(extra)
        return payload

    def _emit_agent_activity(
        self,
        agent: str,
        activity: str,
        **metadata: Any,
    ) -> None:
        """Emite atividade operacional vinculada visualmente a um agente."""
        payload = self._agent_event_payload(
            agent,
            {"activity": activity, **metadata},
        )
        self._bridge.emit(TextualUiEvent("agent_activity", payload, agent=agent))

    def notify_agent_retry(
        self,
        agent: str,
        *,
        reason: str,
        attempt: int,
        limit: int,
        detail: str = "",
    ) -> None:
        """Emite nova tentativa de um agente como atividade estruturada.

        Recebe os campos já separados (motivo canônico, tentativa, limite,
        detalhe) direto da camada de execução, sem reconstruir texto. O
        ``reason`` é traduzido pelo mapa único de rótulos.
        """
        self._emit_agent_activity(
            agent,
            "retrying",
            reason=str(reason),
            message=_RETRY_REASON_LABELS.get(str(reason), str(reason)),
            attempt=int(attempt),
            limit=int(limit),
            detail=str(detail or "").strip(),
        )

    def notify_agent_failover(
        self,
        agent: str,
        *,
        target: str,
        message: str = _FAILOVER_DEFAULT_MESSAGE,
    ) -> None:
        """Emite failover de um agente para outro como atividade estruturada."""
        target_style, target_label = self._resolve_agent_style(str(target))
        self._emit_agent_activity(
            agent,
            "failover",
            message=str(message or _FAILOVER_DEFAULT_MESSAGE),
            target=str(target),
            target_label=target_label,
            target_style=target_style,
        )

    def set_prompt_integration(self, is_active_fn, run_above_fn) -> None:
        """Compatibilidade com TerminalRenderer."""
        return None

    def close(self, timeout: float = 5.0) -> None:
        """Compatibilidade com TerminalRenderer."""
        return None

    def log_debug_event(self, event: str, **payload) -> None:
        """Compatibilidade com auditoria do TerminalRenderer."""
        if self._audit_logger is None:
            return
        try:
            self._audit_logger.log_event(event, **payload)
        except Exception:
            return

    @contextmanager
    def terminal_floor(self, *, title: str = "Terminal floor", metadata: dict[str, Any] | None = None, timeout: float = 2.0):
        """Compatibilidade para I/O baixo nível que pede posse do terminal."""
        with self._interactive_window("terminal_floor", title, metadata=metadata):
            yield

    def external_window(self, window_id: str, title: str = "", metadata=None):
        """Entrega temporariamente o terminal para uma janela/processo externo."""
        with self._bridge._lock:
            textual_app = self._bridge.textual_app
        return _external_textual_window(textual_app)

    @contextmanager
    def _interactive_window(self, kind: str, title: str, owner: str | None = None, metadata=None):
        """Sinaliza janela interativa sem ceder stdout fora do Textual."""
        metadata_dict = dict(metadata or {})
        metadata_options = metadata_dict.get("options") or []
        options = list(_APPROVAL_OPTIONS) if kind == "approval" else list(metadata_options or [])
        question = str(metadata_dict.get("question") or "")
        should_show_overlay = kind == "approval" or bool(question) or bool(options)
        self._bridge.begin_direct_input()
        if should_show_overlay:
            self._bridge.emit(
                TextualUiEvent(
                    "window_open",
                    {
                        "kind": kind,
                        "title": title,
                        "owner": owner,
                        "metadata": metadata_dict,
                        "question": question,
                        "options": options,
                    },
                )
            )
        try:
            yield
        finally:
            if should_show_overlay:
                self._bridge.emit(TextualUiEvent("window_clear", {"kind": kind}))
            self._bridge.end_direct_input()

    def approval_window(self, *, title: str = "Permissão solicitada", owner: str | None = None, metadata=None, **kwargs):
        """Compatibilidade com fluxos legados de aprovação."""
        return self._interactive_window("approval", title, owner=owner, metadata=metadata)

    def input_window(self, *, title: str = "Entrada solicitada", owner: str | None = None, metadata=None, **kwargs):
        """Compatibilidade com fluxos legados de entrada."""
        return self._interactive_window("input", title, owner=owner, metadata=metadata)

    def selection_window(
        self,
        *,
        title: str = "Seleção solicitada",
        owner: str | None = None,
        metadata=None,
        options: list[str] | None = None,
        **kwargs,
    ):
        """Compatibilidade com fluxos legados de seleção."""
        metadata_dict = dict(metadata or {})
        if options is not None:
            metadata_dict["options"] = list(options)
        return self._interactive_window("selection", title, owner=owner, metadata=metadata_dict)

    def open_config(self) -> None:
        """Abre a janela popup de configurações."""
        self._bridge.emit(TextualUiEvent("open_config", None))

    def flush(self, timeout: float = 5.0) -> None:
        """Drena eventos visuais pendentes no app Textual."""
        self._bridge.flush_ui_events()

    def flush_quick(self, timeout: float = 0.15) -> bool:
        """Drena eventos visuais pendentes sem bloquear o prompt."""
        return self._bridge.flush_ui_events()

    def show_system(self, message: str) -> None:
        """Exibe mensagem de sistema livre.

        Eventos estruturados (ex.: failover) usam ``notify_agent_failover``;
        este método é só para texto de sistema genuíno.
        """
        clean_message = strip_ansi(str(message)).strip("\r\n")
        self._bridge.emit(TextualUiEvent("system", clean_message))

    def show_banner(self, message: str) -> None:
        """Exibe banner de boas-vindas/logo no feed Textual."""
        self._bridge.emit(TextualUiEvent("banner", strip_ansi(str(message)).strip("\r\n")))

    def show_system_neutral(self, message: str) -> None:
        """Exibe mensagem neutra."""
        self._bridge.emit(TextualUiEvent("muted", str(message)))

    def signal_restore_history(self) -> None:
        """Sinaliza ao feed para exibir o histórico restaurado após as mensagens de startup."""
        self._bridge.emit(TextualUiEvent("restore_history"))

    def show_warning(self, message: str) -> None:
        """Exibe warning livre.

        Novas tentativas de agente usam ``notify_agent_retry``; este método
        permanece para avisos de sistema genuínos.
        """
        clean_message = strip_ansi(str(message)).strip("\r\n")
        self._bridge.emit(TextualUiEvent("warning", clean_message))

    def show_error(self, message: str, **metadata) -> None:
        """Exibe erro."""
        agent = metadata.get("agent")
        command_name = metadata.get("command_name")
        error_kind = metadata.get("error_kind")
        return_code = metadata.get("return_code")
        clean_message = strip_ansi(str(message)).strip("\r\n")
        subject = str(agent or command_name or "").strip()
        if error_kind == "agent_exit" and return_code is not None:
            clean_message = (
                f"[erro] retornou código {return_code}"
                if agent
                else f"[erro] agente {subject or 'unknown'} retornou código {return_code}"
            )
        elif error_kind == "agent_comm":
            clean_message = (
                f"[erro] falha ao comunicar: {clean_message}"
                if agent
                else f"[erro] falha ao comunicar com {subject or 'unknown'}: {clean_message}"
            )
        elif error_kind == "agent_invalid_output":
            clean_message = (
                "[erro] não retornou saída válida"
                if agent
                else f"[erro] agente {subject or 'unknown'} não retornou saída válida"
            )
        self._bridge.emit(TextualUiEvent("error", clean_message, agent=str(agent) if agent else None))

    def show_approval(self, message: str) -> None:
        """Exibe bloco persistente de aprovação no feed."""
        self._bridge.emit(TextualUiEvent("approval", strip_ansi(str(message)).strip("\r\n")))

    def clear_screen(self) -> None:
        """Limpa o feed Textual sem escrever ANSI direto no terminal."""
        self._bridge.emit(TextualUiEvent("clear"))

    def show_plain(self, message: str, agent=None, muted: bool = False) -> None:
        """Exibe texto simples."""
        kind = "tool_preview" if muted and agent else ("muted" if muted else "plain")
        self._bridge.emit(TextualUiEvent(kind, str(message), agent=agent))

    def show_feed(self, message: str, agent=None, muted: bool = False) -> None:
        """Exibe texto no feed."""
        self.show_plain(message, agent=agent, muted=muted)

    def show_turn_summary(self, agent: str | None, detail: dict) -> None:
        """Exibe resumo compacto de tools do turno."""
        runtime = str((detail or {}).get("runtime") or "").strip().lower()
        if runtime and runtime != "cli":
            return
        tools = detail.get("tools", []) if isinstance(detail, dict) else []
        if not isinstance(tools, list) or not tools:
            return
        total = 0
        ok_count = 0
        err_count = 0
        total_ms = 0
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            total += 1
            status = str(tool.get("status") or "").strip().lower()
            if status in {"ok", "success", "succeeded"}:
                ok_count += 1
            if status in {"error", "failed", "fail", "timeout"}:
                err_count += 1
            duration_ms = tool.get("duration_ms")
            if isinstance(duration_ms, int) and duration_ms >= 0:
                total_ms += duration_ms
        if total <= 0:
            return
        duration = f"{total_ms}ms" if total_ms < 1000 else f"{total_ms / 1000:.1f}s"
        payload = self._agent_event_payload(
            agent,
            {
                "total": total,
                "ok_count": ok_count,
                "err_count": err_count,
                "duration": duration,
            },
        )
        self._bridge.emit(TextualUiEvent("turn_summary", payload, agent=agent))

    def show_delegation(self, from_agent, to_agent, task=None, *, delegation_id=None, chain=None) -> None:
        """Exibe delegação entre agentes."""
        from_style, from_label = self._resolve_agent_style(str(from_agent))
        to_style, to_label = self._resolve_agent_style(str(to_agent))
        delegation_chain = [str(item) for item in (chain or []) if str(item).strip()]
        self._bridge.emit(
            TextualUiEvent(
                "delegation",
                {
                    "from_label": from_label,
                    "from_style": from_style,
                    "to_label": to_label,
                    "to_style": to_style,
                    "task": str(task or "").strip(),
                    "delegation_id": str(delegation_id or "").strip(),
                    "chain": delegation_chain,
                },
            )
        )

    def show_agent_lifecycle(self, agent: str, status: str | AgentLifecycleStatus, message: str) -> None:
        """Exibe lifecycle transitório de agente como evento semântico."""
        lifecycle = _agent_lifecycle_payload(message, status=status)
        self._bridge.emit(
            TextualUiEvent(
                "agent_lifecycle",
                self._agent_event_payload(agent, lifecycle),
                agent=str(agent),
            )
        )

    def show_message(self, agent, content, render_mode: str = "auto") -> None:
        """Exibe resposta final de agente com ícone."""
        clean_content = strip_ansi(_extract_text_from_renderable(content))
        self._stream_content_by_agent.pop(str(agent), None)
        self._bridge.clear_agent_active(str(agent))
        self._bridge.emit(
            TextualUiEvent(
                "agent_message",
                self._agent_event_payload(
                    agent,
                    {"content": clean_content, "render_mode": render_mode},
                ),
                agent=str(agent),
            )
        )

    def show_no_response(self, agent) -> None:
        """Exibe ausência de resposta."""
        self.show_message(agent, _NO_RESPONSE_MESSAGE, render_mode="plain")

    def start_message_stream(self, agent) -> None:
        """Inicia stream visual com ícone do agente."""
        style, label = self._resolve_agent_style(str(agent))
        self._bridge.set_agent_active(str(agent), label, style)
        self._stream_content_by_agent[str(agent)] = ""
        self._bridge.emit(
            TextualUiEvent("stream_start", self._agent_event_payload(agent), agent=str(agent))
        )

    def update_message_stream(self, agent, chunk) -> None:
        """Atualiza stream visual."""
        agent_key = str(agent)
        current = self._stream_content_by_agent.get(agent_key, "")
        if isinstance(chunk, dict):
            diff = _normalize_stream_diff(chunk.get("diff"))
            if diff:
                current = _apply_stream_diff(current, diff)
            elif chunk.get("text"):
                current += strip_ansi(str(chunk.get("text")))
            else:
                current += strip_ansi(str(chunk))
        else:
            current += strip_ansi(str(chunk))
        self._stream_content_by_agent[agent_key] = current
        self._bridge.emit(TextualUiEvent("stream_chunk", chunk, agent=str(agent)))

    def finish_message_stream(
        self,
        agent,
        final_content: str,
        render_mode: str = "auto",
    ) -> None:
        """Finaliza stream visual com ícone do agente."""
        self.show_message(agent, final_content, render_mode=render_mode)

    def commit_agent_stream(self, agent, render_mode: str = "auto") -> bool:
        """Compatibilidade com TerminalRenderer."""
        agent_key = str(agent)
        content = self._stream_content_by_agent.get(agent_key, "")
        if not str(content or "").strip():
            return False
        self.show_message(agent_key, content, render_mode=render_mode)
        return True

    def abort_message_stream(self, agent) -> None:
        """Aborta stream visual, se houver stream ativo em andamento.

        Se o stream já foi finalizado (via show_message) antes desta chamada,
        não há nada para abortar e nenhum indicador "interrompido" deve ser
        exibido — evita artefato fixo no feed após limpeza pós-delegate bem-sucedida.
        """
        agent_key = str(agent)
        had_active_stream = agent_key in self._stream_content_by_agent
        self._stream_content_by_agent.pop(agent_key, None)
        self._bridge.clear_agent_active(agent_key)
        if had_active_stream:
            self._bridge.emit(
                TextualUiEvent("stream_abort", self._agent_event_payload(agent), agent=agent_key)
            )

    def update_agent_transient(self, agent, message: str) -> None:
        """Exibe progresso transitório como linha de status."""
        payload = self._agent_event_payload(agent, {"content": str(message)})
        self._bridge.emit(TextualUiEvent("agent_update", payload, agent=str(agent)))

    def clear_agent_transient(self, agent) -> None:
        """Compatibilidade com TerminalRenderer."""
        self._bridge.emit(TextualUiEvent("visual_reset", agent=str(agent)))

    def reset_visual_state(self, agent: str | None = None) -> None:
        """Limpa estados visuais transitórios após cancelamento."""
        if agent:
            self._bridge.clear_agent_active(str(agent))
            self._bridge.emit(TextualUiEvent("visual_reset", agent=str(agent)))
            return
        self._statuses.clear()
        self._bridge.emit(TextualUiEvent("visual_reset"))

    def set_agent_pending_input(self, agent: str, kind: str, question: str = "") -> None:
        """Sinaliza input pendente de agente como card visual."""
        label = "aprovação pendente" if str(kind) == "approval" else "input pendente"
        first_line = str(question or label).strip().splitlines()[0] if str(question or "").strip() else label
        payload = self._agent_event_payload(
            agent,
            {"kind": str(kind or "input"), "question": str(question or first_line)},
        )
        self._bridge.emit(TextualUiEvent("pending_input", payload, agent=str(agent)))

    def clear_agent_pending_input(self, agent: str) -> None:
        """Remove status transitório de input pendente."""
        self.clear_agent_transient(agent)

    def update_status(
        self,
        agent,
        message,
        *,
        status: AgentLifecycleStatus | str = AgentLifecycleStatus.RUNNING,
    ) -> None:
        """Atualiza status de agente paralelo no feed Textual."""
        key = str(agent or "global")
        self._statuses[key] = str(message)
        lifecycle = _agent_lifecycle_payload(message, status=status)
        self._bridge.emit(
            TextualUiEvent(
                "agent_lifecycle",
                self._agent_event_payload(key, lifecycle),
                agent=key,
            )
        )

    @contextmanager
    def live_status(self, agents):
        """Context manager de status para múltiplos agentes."""
        for agent in agents or []:
            self.update_status(agent, "inicializando...")
        try:
            yield
        finally:
            for agent in agents or []:
                self.update_status(agent, "concluído", status=AgentLifecycleStatus.COMPLETED)

    def running_status(self, initial="", agent=None):
        """Retorna context manager compatível com status Rich."""
        return _TextualStatus(self, str(agent) if agent else None, str(initial or ""))

    def show_newline(self) -> None:
        """Exibe linha vazia."""
        self._bridge.emit(TextualUiEvent("plain", ""))

    def show_prompt_preview(self, agent: str, preview: str) -> None:
        """Exibe preview de prompt."""
        self._bridge.emit(TextualUiEvent("plain", preview, agent=agent))

    def set_summarizing(self, active: bool) -> None:
        """Sinaliza início/fim de sumarização para animação no header."""
        self._bridge.emit(TextualUiEvent("summarizing", active))
