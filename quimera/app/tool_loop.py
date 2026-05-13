"""ToolLoopService — gerencia o loop de execução de ferramentas após resposta inicial de agente."""
import re

from ..runtime.errors import (
    ToolError,
    ToolValidationError,
    ToolEnvironmentError,
    ToolLogicError,
    ToolRateLimitError,
)
from ..runtime.tool_hops import get_invalid_tool_loop_threshold, get_max_tool_hops
from ..runtime.parser import strip_tool_block
from .task_utils import truncate_payload
from .config import logger


def _coerce_tool_error(error):
    """Normaliza strings cruas em ToolError quando houver heurística compatível."""
    if not error or isinstance(error, ToolError):
        return error
    error_msg = str(error)
    lowered = error_msg.lower()
    if "validação" in lowered or "campo" in lowered or "formato" in lowered:
        return ToolValidationError(error_msg)
    if "arquivo" in lowered or "permissão" in lowered or "não encontrado" in lowered:
        return ToolEnvironmentError(error_msg)
    if "regra" in lowered or "lógica" in lowered or "contradiz" in lowered:
        return ToolLogicError(error_msg)
    if "rate limit" in lowered or "throttling" in lowered:
        return ToolRateLimitError(error_msg)
    return error


def _resolve_tool_error_type(tool_result) -> str:
    """Resolve o tipo de erro padronizado quando disponível."""
    error_type = getattr(tool_result, "error_type", None)
    if isinstance(error_type, str) and error_type:
        return error_type
    return "none"


def _invalid_tool_signature(tool_result, error_type: str) -> tuple[str, str, str]:
    """Gera assinatura estável para detectar repetição do mesmo erro de policy."""
    tool_name = str(getattr(tool_result, "tool_name", "") or "")
    error_text = re.sub(r"\s+", " ", str(getattr(tool_result, "error", "") or "").strip().lower())
    if len(error_text) > 256:
        error_text = error_text[:256]
    return error_type, tool_name, error_text


class ToolLoopService:
    """Gerencia o loop de execução de ferramentas até estabilizar a saída.

    Sem dependência direta em QuimeraApp — todas as dependências são injetadas.
    """

    _MAX_TOOL_HISTORY_ENTRIES = 3
    _MAX_HANDOFF_CHARS = 8000

    def __init__(
        self,
        tool_executor,
        plugin_resolver,
        call_agent_fn,
        print_response_fn,
        persist_message_fn,
        cancel_checker,
        record_tool_event=None,
        reset_approve_all=None,
        progress_callback=None,
    ):
        """Inicializa ToolLoopService com dependências explícitas.

        Args:
            tool_executor: objeto com maybe_execute_from_response(response).
            plugin_resolver: callable(agent_name) -> plugin.
            call_agent_fn: callable(agent, handoff, primary, protocol_mode, silent) -> str|None.
            print_response_fn: callable(agent, text) para exibir texto visível ao usuário.
            persist_message_fn: callable(agent, text) para persistir mensagem no histórico.
            cancel_checker: callable() -> bool, True se o usuário cancelou.
            record_tool_event: callable(agent, **kwargs) para métricas (opcional).
            reset_approve_all: callable() chamado no finally (opcional).
        """
        self._tool_executor = tool_executor
        self._plugin_resolver = plugin_resolver
        self._call_agent_fn = call_agent_fn
        self._print_response_fn = print_response_fn
        self._persist_message_fn = persist_message_fn
        self._cancel_checker = cancel_checker
        self._record_tool_event = record_tool_event or (lambda *a, **kw: None)
        self._reset_approve_all = reset_approve_all or (lambda: None)
        self._progress_callback = progress_callback or (lambda *a, **kw: None)

    def execute(self, agent, response, silent=False, persist_history=True, show_output=True):
        """Executa o loop de ferramentas até estabilizar a saída.

        Returns:
            Resposta final estabilizada, mensagem de erro de abort, ou None se cancelado.
        """
        current_response = response
        plugin = self._plugin_resolver(agent)
        max_tool_hops = get_max_tool_hops(getattr(plugin, "tool_use_reliability", "medium"))
        max_consecutive_invalid_signatures = get_invalid_tool_loop_threshold(
            getattr(plugin, "tool_use_reliability", "medium")
        )
        tool_history = []
        last_invalid_signature = None
        consecutive_invalid_signature_count = 0

        try:
            for hop in range(max_tool_hops):
                if self._cancel_checker():
                    logger.info(
                        "[TOOL_LOOP] agent=%s cancelled by user during tool loop at hop=%d/%d, aborting",
                        agent, hop + 1, max_tool_hops,
                    )
                    return None
                if not current_response:
                    return current_response

                raw_response, tool_result = self._tool_executor.maybe_execute_from_response(current_response)

                if tool_result is None:
                    return current_response

                if tool_result.error:
                    tool_result.error = _coerce_tool_error(tool_result.error)
                error_type = _resolve_tool_error_type(tool_result)
                is_invalid = error_type == "policy"
                ok = bool(getattr(tool_result, "ok", False))

                self._record_tool_event(agent, ok=ok, is_invalid=is_invalid, error_type=error_type)

                if is_invalid:
                    invalid_signature = _invalid_tool_signature(tool_result, error_type)
                    if last_invalid_signature == invalid_signature:
                        consecutive_invalid_signature_count += 1
                    else:
                        last_invalid_signature = invalid_signature
                        consecutive_invalid_signature_count = 1
                    if consecutive_invalid_signature_count >= max_consecutive_invalid_signatures:
                        self._record_tool_event(
                            agent,
                            ok=False,
                            is_invalid=True,
                            loop_abort=True,
                            reason="invalid_tool_loop",
                            error_type=error_type,
                        )
                        return "Falha: loop de ferramenta inválida detectado."
                else:
                    last_invalid_signature = None
                    consecutive_invalid_signature_count = 0

                tool_payload = truncate_payload(tool_result.to_model_payload())
                tool_history.append(
                    f"Sua resposta anterior:\n{current_response.strip()}\n\n"
                    f"Resultado da ferramenta:\n{tool_payload}"
                )
                if len(tool_history) > self._MAX_TOOL_HISTORY_ENTRIES:
                    tool_history = tool_history[-self._MAX_TOOL_HISTORY_ENTRIES:]

                visible_text = strip_tool_block(raw_response or "")
                if visible_text:
                    if show_output:
                        self._print_response_fn(agent, visible_text)
                    if persist_history:
                        self._persist_message_fn(agent, visible_text)
                
                # Report progress for this tool hop
                self._progress_callback(
                    agent=agent,
                    tool_name=getattr(tool_result, "tool_name", "unknown"),
                    hop=hop + 1,
                    max_hops=max_tool_hops,
                    elapsed=0.0,  # We don't have timing info here
                    ok=ok,
                    is_invalid=is_invalid
                )

                used_tool_hops = hop + 1
                remaining_tool_hops = max(max_tool_hops - used_tool_hops, 0)
                followup_handoff = (
                    "Orçamento de ferramentas desta execução:\n"
                    f"- max_tool_hops={max_tool_hops}\n"
                    f"- remaining_tool_hops={remaining_tool_hops}\n\n"
                    "Histórico de ferramentas desta rodada:\n\n"
                    + "\n\n---\n\n".join(tool_history)
                )
                if len(followup_handoff) > self._MAX_HANDOFF_CHARS:
                    followup_handoff = followup_handoff[-self._MAX_HANDOFF_CHARS:]
                    followup_handoff = "(histórico truncado)...\n\n" + followup_handoff

                if self._cancel_checker():
                    logger.info(
                        "[TOOL_LOOP] agent=%s cancelled by user before followup tool call, aborting",
                        agent,
                    )
                    return None

                current_response = self._call_agent_fn(
                    agent,
                    handoff=followup_handoff,
                    primary=False,
                    protocol_mode="tool_loop",
                    silent=silent,
                )

                if self._cancel_checker():
                    logger.info(
                        "[TOOL_LOOP] agent=%s cancelled by user after followup tool call, aborting",
                        agent,
                    )
                    return None

            self._record_tool_event(agent, ok=False, loop_abort=True, reason="max_tool_hops")
            return "Falha: limite de execuções de ferramenta atingido."
        finally:
            self._reset_approve_all()
