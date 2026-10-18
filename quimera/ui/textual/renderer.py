"""Renderer compatível com o contrato legado sobre eventos Textual."""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import quimera.themes as themes
from quimera.ui.text import (
    _extract_text_from_renderable,
    strip_ansi,
)
from quimera.ui.base import RendererBase
from quimera.ui.boot_status import coerce_boot_status_lines
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
from quimera.domain.agent_run_scope import current_agent_run_scope
from quimera.domain.execution import ExecutionControlEvent
from quimera.domain.tool_activity import count_tool_activities

_logger = logging.getLogger(__name__)

# Execuções encerradas sem resposta final mantidas por agente (ver
# ``_prune_closed_runs_locked``).
_MAX_TERMINAL_RUNS_PER_AGENT = 4
_MAX_REMEMBERED_CLOSED_RUNS = 256


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
    supports_submission_status = True
    supports_structured_agent_activity = True
    supports_screenshot = True

    def __init__(self, bridge: TextualUiBridge) -> None:
        self._bridge = bridge
        self._audit_logger = None
        self._console = _TextualConsoleShim(bridge)
        self._profile_resolver: Callable | None = None
        self._theme = themes.get(themes.DEFAULT_THEME)
        self._statuses: dict[str, str] = {}
        # Execuções abertas de cada agente, por run_id, em ordem de início.
        # O mesmo agente pode ter várias ao mesmo tempo (chat, task, delegação).
        self._open_runs_by_agent: dict[str, dict[str, dict[str, str]]] = {}
        # Fechadas há pouco: um evento tardio delas (limpeza do delegador, por
        # exemplo) continua sendo delas, e não da outra execução aberta.
        self._closed_run_ids: OrderedDict[str, None] = OrderedDict()
        self._run_context_lock = threading.RLock()
        self._orchestrator_agent: str | None = None

    @property
    def theme_name(self) -> str:
        """Retorna o nome do tema ativo."""
        return self._theme.name

    def cycle_theme(self) -> str:
        """Avança para o próximo tema de `themes.py` e notifica a UI."""
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

    def update_submission_status(self, submission_id: str, status: str, **metadata):
        """Encaminha o lifecycle do prompt para o turno humano no feed."""
        return self._bridge.update_submission_status(submission_id, status, **metadata)

    def set_orchestrator(self, agent_name: str | None) -> None:
        """Define qual agente é o orquestrador ativo."""
        self._orchestrator_agent = str(agent_name).lower().strip() if agent_name else None

    def _resolve_agent_label(self, agent: str) -> str:
        """Retorna label formatada com ícone do agente, ex: '🔮  Claude'."""
        _style, label = self._resolve_agent_style(agent)
        return label

    def _resolve_agent_style(self, agent: str) -> tuple[str, str]:
        """Retorna (style, label) para o agente usando o resolver existente."""
        if str(agent or "").strip().lower() == "mcp-http":
            return "cyan", "🤖  mcp-http"
        resolver = self._profile_resolver
        if resolver:
            try:
                result = resolver(str(agent).lower())
                if result:
                    style, label = result
                    return str(style or "cyan"), str(label)
            except Exception as exc:
                _logger.debug("resolver de estilo do agente %s falhou: %s", agent, exc)
        agent_name = str(agent).capitalize() if agent else "Agente"
        return "cyan", f"🤖  {agent_name}"

    def _agent_event_payload(
        self,
        agent,
        extra: dict[str, Any] | None = None,
        *,
        include_run_context: bool = True,
    ) -> dict[str, Any]:
        """Monta payload visual comum para eventos de agente."""
        agent_name = str(agent or "")
        style, label = self._resolve_agent_style(agent_name)
        payload = {"label": label, "style": style, "theme": self._theme.name}
        explicit_identity = any((extra or {}).get(key) for key in ("run_id", "delegation_id"))
        if include_run_context and not explicit_identity:
            # Identidade explícita vence: misturar o contexto de outra execução
            # (transport, delegation_id) corromperia a chave do cartão.
            payload.update(self._agent_run_context(agent_name))
        if self._orchestrator_agent and agent_name.lower().strip() == self._orchestrator_agent:
            payload["orchestrator"] = True
        if extra:
            payload.update(extra)
        return payload

    @staticmethod
    def _is_terminal_run(context: dict[str, str]) -> bool:
        return str(context.get("status") or "") in {"finished", "failed", "cancelled"}

    def _resolve_open_run_locked(self, agent_name: str) -> tuple[str, dict[str, str]] | None:
        """Execução a que um evento sem identidade explícita pertence.

        1. A execução do contexto corrente (thread/escopo do gateway), se este
           renderer a conhece, mesmo fechada há pouco: o evento é dela.
        2. Sem escopo conhecido no contexto, a única execução aberta do agente.
        Com várias abertas e nenhum escopo, não há como saber: ``None``.
        """
        runs = self._open_runs_by_agent.get(agent_name) or {}
        scope = current_agent_run_scope(agent_name)
        if scope is not None:
            context = runs.get(scope.run_id)
            if context is not None:
                return scope.run_id, context
            if scope.run_id in self._closed_run_ids:
                return scope.run_id, {"run_id": scope.run_id, **scope.as_payload()}
            # Escopo que este renderer desconhece (resto de uma execução antiga
            # nesta thread, run de MCP que não é a execução visual): não prova
            # a qual execução o evento pertence.
        if not runs:
            return None
        if len(runs) == 1:
            run_id, context = next(iter(runs.items()))
            return run_id, context
        live = [(run_id, context) for run_id, context in runs.items() if not self._is_terminal_run(context)]
        if len(live) == 1:
            return live[0]
        return None

    def _agent_run_context(self, agent: str) -> dict[str, str]:
        with self._run_context_lock:
            resolved = self._resolve_open_run_locked(str(agent or ""))
            return dict(resolved[1]) if resolved is not None else {}

    def _open_run_ids(self, agent: str) -> list[str]:
        with self._run_context_lock:
            return [run_id for run_id in self._open_runs_by_agent.get(str(agent or "")) or {} if run_id]

    def _clear_agent_run_context(
        self,
        agent: str,
        *,
        run_id: str = "",
        only_terminal: bool = False,
    ) -> None:
        """Fecha uma execução do agente; nunca as demais que seguem abertas."""
        agent_name = str(agent or "")
        with self._run_context_lock:
            runs = self._open_runs_by_agent.get(agent_name)
            if not runs:
                return
            expected = str(run_id or "").strip()
            if not expected:
                resolved = self._resolve_open_run_locked(agent_name)
                if resolved is None:
                    return
                expected = resolved[0]
            current = runs.get(expected)
            if current is None or (only_terminal and not self._is_terminal_run(current)):
                return
            runs.pop(expected, None)
            self._remember_closed_run_locked(expected)
            if not runs:
                self._open_runs_by_agent.pop(agent_name, None)

    def begin_agent_run(
        self,
        agent,
        *,
        run_id: str = "",
        parent_run_id: str = "",
        delegation_id: str = "",
        transport: str = "",
    ) -> None:
        context = {
            key: value
            for key, value in {
                "run_id": str(run_id or "").strip(),
                "parent_run_id": str(parent_run_id or "").strip(),
                "delegation_id": str(delegation_id or "").strip(),
                "transport": str(transport or "").strip(),
            }.items()
            if value
        }
        if not context:
            return
        agent_name = str(agent or "")
        with self._run_context_lock:
            runs = self._open_runs_by_agent.setdefault(agent_name, {})
            runs.pop(context.get("run_id", ""), None)
            runs[context.get("run_id", "")] = context
            self._prune_closed_runs_locked(runs)

    def _prune_closed_runs_locked(self, runs: dict[str, dict[str, str]]) -> None:
        """Descarta as encerradas mais antigas que nunca receberam resposta final.

        Execuções silenciosas terminam sem ``show_message``; sem o corte elas
        se acumulariam e tornariam ambígua a resolução por agente.
        """
        terminal = [run_id for run_id, context in runs.items() if self._is_terminal_run(context)]
        for run_id in terminal[: max(0, len(terminal) - _MAX_TERMINAL_RUNS_PER_AGENT)]:
            runs.pop(run_id, None)
            self._remember_closed_run_locked(run_id)

    def _remember_closed_run_locked(self, run_id: str) -> None:
        if not run_id:
            return
        self._closed_run_ids.pop(run_id, None)
        self._closed_run_ids[run_id] = None
        while len(self._closed_run_ids) > _MAX_REMEMBERED_CLOSED_RUNS:
            self._closed_run_ids.popitem(last=False)

    def end_agent_run(self, agent, *, run_id: str = "", status: str = "") -> None:
        agent_name = str(agent or "")
        with self._run_context_lock:
            expected = str(run_id or "").strip()
            if not expected:
                resolved = self._resolve_open_run_locked(agent_name)
                if resolved is None:
                    return
                expected = resolved[0]
            current = (self._open_runs_by_agent.get(agent_name) or {}).get(expected)
            if current is None:
                return
            if status:
                current["status"] = str(status)

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

    def set_audit_logger(self, audit_logger) -> None:
        """Liga o audit de render (``--debug``) a este renderer.

        O logger chega depois da construção, porque a TUI cria o renderer
        antes de o app saber se está em debug. Sem esta ligação ``log_debug_event`` era no-op e o jsonl da
        sessão ficava vazio no modo Textual.
        """
        self._audit_logger = audit_logger

    def close(self, timeout: float = 5.0) -> None:
        """Fecha o audit de render, se houver; a UI em si é do app Textual."""
        audit_logger = self._audit_logger
        self._audit_logger = None
        if audit_logger is None:
            return None
        try:
            audit_logger.close()
        except Exception as exc:
            _logger.debug("audit de render: fechar falhou: %s", exc)
            return None
        return None

    def log_debug_event(self, event: str, **payload) -> None:
        """Grava um evento estruturado no audit de render (só com ``--debug``)."""
        if self._audit_logger is None:
            return
        try:
            self._audit_logger.log_event(event, **payload)
        except Exception as exc:
            _logger.debug("audit de render: registrar %s falhou: %s", event, exc)
            return

    def capture_screenshot(self, *, title: str | None = None, timeout: float = 10.0):
        """Captura a tela atual da TUI (texto, SVG e geometria do feed)."""
        return self._bridge.capture_screenshot(title=title, timeout=timeout)

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

    def open_connection_config(self, agent_name: str, *, advanced: bool = False) -> bool:
        """Solicita ao app Textual a abertura do modal de conexão."""
        self._bridge.emit(
            TextualUiEvent(
                "open_connection_config",
                {"agent": agent_name, "advanced": bool(advanced)},
                agent=agent_name,
            )
        )
        return True

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
        """Exibe banner de boas-vindas/logo no cabeçalho de boot do feed."""
        self._bridge.emit(
            TextualUiEvent(
                "banner",
                strip_ansi(str(message)).strip("\r\n"),
                compact=True,
                boot=True,
            )
        )

    def show_boot_message(self, message: str) -> None:
        """Exibe uma linha compacta pertencente apenas ao cabeçalho de boot.

        Quebras de linha nas bordas saem: um
        ``\\n`` final (ex.: na linha do audit de render) viraria uma linha
        vazia entre o cabeçalho e a régua que o fecha.
        """
        clean_message = strip_ansi(str(message)).strip("\r\n")
        self._bridge.emit(TextualUiEvent("muted", clean_message, compact=True, boot=True))

    def show_boot_status(self, key: str, lines) -> None:
        """Cria ou substitui no lugar o bloco de status do boot ``key``."""
        payload = {
            "key": str(key),
            "lines": [line.as_payload() for line in coerce_boot_status_lines(lines)],
        }
        self._bridge.emit(TextualUiEvent("boot_status", payload, compact=True, boot=True))

    def show_system_neutral(self, message: str) -> None:
        """Exibe mensagem neutra com o espaçamento normal do feed."""
        self._bridge.emit(TextualUiEvent("muted", str(message)))

    def show_notification(self, message: str, *, severity: str = "information", timeout: float | None = None) -> None:
        """Exibe informação fora do feed, via toast da TUI."""
        payload = {
            "message": strip_ansi(str(message)).strip("\r\n"),
            "severity": severity,
            "timeout": timeout,
        }
        self._bridge.emit(TextualUiEvent("notification", payload))

    def show_execution_control(self, event: ExecutionControlEvent) -> None:
        """Encaminha controle de execução sem converter o evento em texto."""
        self._bridge.emit(TextualUiEvent("execution_control", event, agent=event.agent))

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
        """Limpa o chat do feed Textual, preservando o cabeçalho de boot.

        Não escreve ANSI no terminal: o app Textual remove só os itens que
        não pertencem ao cabeçalho (logo, projeto, bloco MCP, caminhos de log).
        """
        self._bridge.emit(TextualUiEvent("clear"))

    def show_plain(self, message: str, agent=None, muted: bool = False) -> None:
        """Exibe texto simples."""
        if muted and agent:
            self.show_tool_preview(str(message), agent=agent)
            return
        kind = "muted" if muted else "plain"
        self._bridge.emit(TextualUiEvent(kind, str(message), agent=agent))

    def show_feed(self, message: str, agent=None, muted: bool = False) -> None:
        """Exibe texto no feed."""
        self.show_plain(message, agent=agent, muted=muted)

    def show_tool_preview(self, message, *, agent=None, metadata=None) -> None:
        """Exibe preview de tool preservando metadados de run no Textual."""
        del metadata
        if not agent:
            self.show_plain(message, agent=agent, muted=True)
            return
        extra = dict(message) if isinstance(message, dict) else {"content": str(message)}
        payload = self._agent_event_payload(agent, extra)
        self._bridge.emit(TextualUiEvent("tool_preview", payload, agent=str(agent)))

    def show_tool_run_state(self, agent, detail) -> None:
        """Atualiza o estado visual de uma tool MCP HTTP concluída."""
        payload = self._agent_event_payload(
            agent,
            dict(detail) if isinstance(detail, dict) else {},
        )
        self._bridge.emit(TextualUiEvent("tool_state", payload, agent=str(agent or "mcp-http")))

    def show_turn_summary(self, agent: str | None, detail: dict) -> None:
        """Exibe resumo compacto de tools do turno."""
        runtime = str((detail or {}).get("runtime") or "").strip().lower()
        if runtime and runtime not in {"cli", "openai"}:
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
                "activity_counts": count_tool_activities(tools),
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
        payload = self._agent_event_payload(
            agent,
            {"content": clean_content, "render_mode": render_mode},
        )
        agent_key = str(agent)
        self._bridge.clear_agent_active(agent_key)
        self._bridge.emit(
            TextualUiEvent(
                "agent_message",
                payload,
                agent=agent_key,
            )
        )
        self._clear_agent_run_context(agent_key, run_id=str(payload.get("run_id") or ""))

    def show_no_response(self, agent) -> None:
        """Exibe ausência de resposta."""
        self.show_message(agent, _NO_RESPONSE_MESSAGE, render_mode="plain")

    def abort_message_stream(self, agent, *, run_id: str = "") -> None:
        """Encerra o estado visual do agente após limpeza externa da execução."""
        agent_key = str(agent)
        if not run_id:
            run_id = str(self._agent_event_payload(agent_key).get("run_id") or "")
        self._bridge.clear_agent_active(agent_key)
        self._clear_agent_run_context(agent_key, run_id=run_id)

    def update_agent_transient(
        self,
        agent,
        message: str,
        *,
        run_id: str = "",
        parent_run_id: str = "",
        delegation_id: str = "",
        transport: str = "",
        answer: bool = False,
    ) -> None:
        """Exibe progresso transitório como linha de status.

        ``answer`` marca a resposta parcial do agente, que não é raciocínio.
        """
        extra = {
            key: value
            for key, value in {
                "content": str(message),
                "run_id": str(run_id or "").strip(),
                "parent_run_id": str(parent_run_id or "").strip(),
                "delegation_id": str(delegation_id or "").strip(),
                "transport": str(transport or "").strip(),
                "answer": bool(answer),
            }.items()
            if value
        }
        payload = self._agent_event_payload(
            agent,
            extra,
            include_run_context=not bool(run_id or delegation_id),
        )
        self._bridge.emit(TextualUiEvent("agent_update", payload, agent=str(agent)))

    def clear_agent_transient(
        self,
        agent,
        *,
        run_id: str = "",
        parent_run_id: str = "",
        delegation_id: str = "",
        transport: str = "",
    ) -> None:
        """Parte do contrato de RendererBase."""
        agent_name = str(agent)
        extra = {
            key: value
            for key, value in {
                "run_id": str(run_id or "").strip(),
                "parent_run_id": str(parent_run_id or "").strip(),
                "delegation_id": str(delegation_id or "").strip(),
                "transport": str(transport or "").strip(),
            }.items()
            if value
        }
        payload = self._agent_event_payload(
            agent_name,
            extra,
            include_run_context=not bool(run_id or delegation_id),
        )
        scoped_run_id = str(run_id or payload.get("run_id") or "")
        # Limpar o transitório no meio do turno (CLI, presenter) não encerra a
        # execução; ela só sai do registro depois de terminar.
        self._clear_agent_run_context(agent_name, run_id=scoped_run_id, only_terminal=True)
        self._emit_agent_visual_reset(agent_name, payload)

    def _emit_agent_visual_reset(self, agent_name: str, payload: dict[str, Any]) -> None:
        """Emite o reset visual de um agente sem atingir as outras execuções dele.

        Sem ``run_id``, o feed apagaria todos os cartões do agente; as
        execuções que seguem abertas vão em ``keep_run_ids``.
        """
        if not payload.get("run_id") and not payload.get("delegation_id"):
            keep = self._open_run_ids(agent_name)
            if keep:
                payload = {**payload, "keep_run_ids": keep}
        self._bridge.emit(TextualUiEvent("visual_reset", payload, agent=agent_name))

    def reset_visual_state(self, agent: str | None = None) -> None:
        """Limpa estados visuais transitórios após cancelamento."""
        if agent:
            agent_name = str(agent)
            payload = self._agent_event_payload(agent_name)
            self._bridge.clear_agent_active(agent_name)
            self._clear_agent_run_context(agent_name, run_id=str(payload.get("run_id") or ""))
            self._emit_agent_visual_reset(agent_name, payload)
            return
        self._statuses.clear()
        with self._run_context_lock:
            for runs in self._open_runs_by_agent.values():
                for run_id in runs:
                    self._remember_closed_run_locked(run_id)
            self._open_runs_by_agent.clear()
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
        """Solicita a exibição do preview em uma janela modal."""
        self._bridge.emit(
            TextualUiEvent(
                "prompt_preview",
                {"agent": str(agent), "preview": str(preview)},
                agent=agent,
            )
        )

    def show_text_window(self, title: str, content: str) -> None:
        """Solicita a exibição de texto longo em uma janela modal."""
        self._bridge.emit(
            TextualUiEvent(
                "text_window",
                {"title": str(title), "content": str(content)},
            )
        )

    def set_summarizing(self, active: bool) -> None:
        """Sinaliza início/fim de sumarização para animação no header."""
        self._bridge.emit(TextualUiEvent("summarizing", active))
