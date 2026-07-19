"""
Driver para endpoints compatíveis com a API OpenAI: Ollama, OpenRouter, LM Studio, etc.

Suporta tool calling nativo e streaming interno (coleta tokens sem bloquear no timeout).
A exibição da resposta final segue o pipeline normal do app (show_message).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

from ...evidence import Evidence, EvidenceStore
from ..approval_broker import TrustedToolExecutionContext
from ...prompt_templates import PromptText
from ..streaming import apply_stream_diff, normalize_stream_diff
from ..tool_hops import (
    DEFAULT_MAX_TOOL_HOPS,
    MAX_TOOL_HOPS_BY_RELIABILITY,  # noqa: F401
    get_invalid_tool_loop_threshold,
    get_max_tool_hops,
)
from .tool_schemas import resolve_tool_schemas
from .prompt_adapter import (
    _build_openai_messages_from_prompt,
    _build_tool_budget_prompt,
    _build_tool_system_prompt,
)
from ..models import ToolCall, ToolResult

MAX_TOOL_HOPS = DEFAULT_MAX_TOOL_HOPS

try:
    from openai import OpenAI
    from openai import (
        AuthenticationError as _OAIAuthError,
        NotFoundError as _OAINotFoundError,
        BadRequestError as _OAIBadRequestError,
        RateLimitError as _OAIRateLimitError,
    )
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]
    _OAIAuthError = Exception  # type: ignore[assignment,misc]
    _OAINotFoundError = Exception  # type: ignore[assignment,misc]
    _OAIBadRequestError = Exception  # type: ignore[assignment,misc]
    _OAIRateLimitError = Exception  # type: ignore[assignment,misc]


def _fatal_api_error_message(exc: Exception) -> str | None:
    """
    Retorna mensagem amigável para erros fatais da API OpenAI-compatible.
    Erros fatais não devem ser retryados — o modelo/chave/request é inválido.
    Retorna None se o erro não for considerado fatal (pode ser transitório).
    """
    if _OAINotFoundError is not Exception and isinstance(exc, _OAINotFoundError):
        body = getattr(exc, "body", None) or {}
        provider = ""
        if isinstance(body, dict):
            meta = body.get("error", {}).get("metadata", {}) if isinstance(body.get("error"), dict) else {}
            provider_name = meta.get("provider_name", "") if isinstance(meta, dict) else ""
            if provider_name:
                provider = f" (provider: {provider_name})"
        return f"Erro fatal: modelo não encontrado{provider}. Verifique o nome do modelo configurado."
    if _OAIAuthError is not Exception and isinstance(exc, _OAIAuthError):
        return "Erro fatal: falha de autenticação na API. Verifique a chave de API configurada."
    if _OAIBadRequestError is not Exception and isinstance(exc, _OAIBadRequestError):
        return f"Erro fatal: requisição inválida — {exc}"
    return None

_logger = logging.getLogger(__name__)

# Remove blocos <think>...</think> ou <thinking>...</thinking> que modelos Qwen3 emitem.
_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL)

# Trunca tool results para evitar explosão de memória no array messages.
_MAX_TOOL_RESULT_CHARS = 32_000
_MAX_TOOL_LOOP_MESSAGES = 24

# Limite padrão de conexões concorrentes ao backend OpenAI-compatible.
# Evita estouro de rate-limit quando múltiplos agentes chamam a API em paralelo.
DEFAULT_MAX_CONNECTIONS = 4


def _strip_thinking(
    text: str,
    *,
    agent_name: str | None = None,
    session_id: str | None = None,
    base_dir: str | Path | None = None,
) -> str:
    """Remove thinking."""
    if text and agent_name and session_id and base_dir is not None:
        evidences: list[Evidence] = []
        for match in _THINK_RE.finditer(text):
            content = match.group(0)
            inner = re.sub(r"^<think(?:ing)?>|</think(?:ing)?>$", "", content, flags=re.DOTALL).strip()
            if not inner:
                continue
            evidences.append(
                Evidence(
                    type="think_summary",
                    agent=agent_name,
                    session_id=session_id,
                    summary=inner[:500],
                    ts=str(time.time()),
                    path="",
                    digest="",
                )
            )
        if evidences:
            with EvidenceStore(Path(base_dir), session_id) as store:
                for evidence in evidences:
                    store.append(evidence)
    return _THINK_RE.sub("", text).strip()


def _sanitize_assistant_text(
    text: str,
    *,
    agent_name: str | None = None,
    session_id: str | None = None,
    base_dir: str | Path | None = None,
) -> str:
    """Remove apenas blocos de thinking; demais texto do assistente é preservado."""
    return _strip_thinking(text, agent_name=agent_name, session_id=session_id, base_dir=base_dir).strip()


def _invalid_tool_signature(result: ToolResult) -> tuple[str, str, str]:
    """Gera assinatura estável para detectar repetição do mesmo erro de policy."""
    error_text = re.sub(r"\s+", " ", str(result.error or "").strip().lower())
    if len(error_text) > 256:
        error_text = error_text[:256]
    return result.error_type, result.tool_name, error_text


def _prune_tool_loop_messages(messages: list[dict]) -> list[dict]:
    """Limita o histórico do loop de tools preservando system/user e a cauda recente."""
    if len(messages) <= _MAX_TOOL_LOOP_MESSAGES:
        return messages
    head_end = 0
    for msg in messages:
        if msg.get("role") in {"system", "user"}:
            head_end += 1
            continue
        break
    if head_end == 0:
        head_end = min(2, len(messages))
    head = messages[:head_end]
    available = max(_MAX_TOOL_LOOP_MESSAGES - len(head), 0)
    tail = messages[len(head):]
    segments: list[list[dict]] = []
    index = 0

    while index < len(tail):
        msg = tail[index]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            segment = [msg]
            index += 1
            while index < len(tail) and tail[index].get("role") == "tool":
                segment.append(tail[index])
                index += 1
            segments.append(segment)
            continue
        segments.append([msg])
        index += 1

    kept_segments: list[list[dict]] = []
    used = 0
    for segment in reversed(segments):
        seg_len = len(segment)
        if kept_segments and used + seg_len > available:
            break
        if not kept_segments and seg_len > available:
            if (
                available >= 2
                and segment[0].get("role") == "assistant"
                and segment[0].get("tool_calls")
            ):
                kept_tools = segment[-(available - 1):]
                kept_ids = {
                    msg.get("tool_call_id")
                    for msg in kept_tools
                    if msg.get("role") == "tool"
                }
                assistant = dict(segment[0])
                assistant["tool_calls"] = [
                    call for call in assistant.get("tool_calls", [])
                    if call.get("id") in kept_ids
                ]
                kept_segments.append([assistant, *kept_tools])
            break
        kept_segments.append(segment)
        used += seg_len

    pruned_tail = [msg for segment in reversed(kept_segments) for msg in segment]
    tail = pruned_tail if pruned_tail else tail[-available:] if available else []
    return head + tail



class _SecretStr:
    """Máscara string sensível em repr/logs."""
    def __init__(self, value: str) -> None:
        self._value = value

    def __repr__(self) -> str:
        return "'******'"

    def __str__(self) -> str:
        return self._value

    def get_secret_value(self) -> str:
        return self._value


class OpenAICompatDriver:
    """Driver para qualquer endpoint compatível com OpenAI.

    Uso com Ollama local:
        driver = OpenAICompatDriver(
            model="qwen3-coder:30b",
            base_url="http://localhost:11434/v1",
        )

    Uso com OpenAI:
        driver = OpenAICompatDriver(
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="sk-...",
        )

    Um semáforo por instância (_semaphore) limita o número de chamadas
    concorrentes ao backend para evitar estouro de rate-limit.
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str = "ollama",
        timeout: Optional[int] = None,
        tool_use_reliability: str = "medium",
        extra_body: Optional[dict] = None,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
    ) -> None:
        """Inicializa uma instância de OpenAICompatDriver.
        extra_body: dicionário opcional mesclado no corpo da requisição (ex: {"thinking": {"type": "enabled"}}).
        max_connections: limite de chamadas concorrentes ao backend (padrão: 4)."""
        self._semaphore = threading.Semaphore(max_connections)
        if OpenAI is None:
            raise ImportError(
                "O pacote 'openai' é dependência obrigatória da instalação. "
                "Reinstale o projeto com: pip install -e ."
            )
        self.model = model
        self._api_key = _SecretStr(api_key)
        self._client = OpenAI(
            base_url=base_url,
            api_key=self._api_key.get_secret_value(),
            timeout=float(timeout) if timeout else 300.0,
        )
        self.tool_use_reliability = str(tool_use_reliability or "medium").lower()
        self.extra_body = dict(extra_body) if extra_body else None

    def run(
            self,
            prompt: PromptText,
            tool_executor=None,
            agent_name: str | None = None,
            parent_agent: str | None = None,
            session_id: str | None = None,
            base_dir: str | Path | None = None,
            on_tool_call=None,
            on_tool_result=None,
            on_tool_abort=None,
            on_text_chunk=None,
            progress_callback=None,
            quiet=False,
            cancel_event=None,
    ) -> Optional[str]:
        """
        Executa o agente com o prompt dado tratando o loop de tool calling internamente.

        Args:
            prompt: Prompt completo construído pelo PromptBuilder.
            tool_executor: Instância de ToolExecutor para executar tool calls.
                           Se None, o agente responde sem ferramentas.
            on_tool_call: Callback opcional chamado antes de cada tool call.
                          Assinatura: on_tool_call(name: str, args: dict) -> None
            on_tool_result: Callback opcional chamado após cada tool result.
                            Assinatura: on_tool_result(result: ToolResult) -> None

        Returns:
            Texto final da resposta do modelo, ou None em caso de falha.
        """
        tools = resolve_tool_schemas(tool_executor) if tool_executor is not None else []
        # Tratamento rápido de cancelamento cooperativo antes de iniciar
        if cancel_event is not None and cancel_event.is_set():
            return None

        # Aguarda slot disponível para evitar estouro de rate-limit no backend
        with self._semaphore:
            if cancel_event is not None and cancel_event.is_set():
                return None

            messages: list[dict] = []
            tool_budget_index: int | None = None
            if tools:
                tool_names = [t["function"]["name"] for t in tools]
                workspace_root = getattr(getattr(tool_executor, "config", None), "workspace_root", None)
                shell_allowlist = getattr(getattr(tool_executor, "config", None), "shell_allowlist", None)
                max_tool_hops = get_max_tool_hops(self.tool_use_reliability)
                messages.append({
                    "role": "system",
                    "content": _build_tool_system_prompt(tool_names, workspace_root, shell_allowlist),
                })
                messages.append({
                    "role": "system",
                    "content": _build_tool_budget_prompt(
                        max_tool_hops=max_tool_hops,
                        remaining_tool_hops=max_tool_hops,
                    ),
                })
                tool_budget_index = len(messages) - 1
            else:
                max_tool_hops = get_max_tool_hops(self.tool_use_reliability)
            messages.extend(_build_openai_messages_from_prompt(prompt))

            last_invalid_signature: tuple[str, str, str] | None = None
            consecutive_invalid_signature_count = 0
            max_consecutive_invalid_signatures = get_invalid_tool_loop_threshold(self.tool_use_reliability)

            try:
                for hop in range(max_tool_hops + 1):
                    if cancel_event is not None and cancel_event.is_set():
                        return None
                    if tool_budget_index is not None:
                        remaining_tool_hops = max(max_tool_hops - hop, 0)
                        messages[tool_budget_index]["content"] = _build_tool_budget_prompt(
                            max_tool_hops=max_tool_hops,
                            remaining_tool_hops=remaining_tool_hops,
                        )
                    try:
                        response_text, tool_calls = self._chat(
                            messages,
                            tools,
                            cancel_event=cancel_event,
                            on_text_chunk=on_text_chunk,
                        )
                    except Exception as exc:
                        _logger.error("OpenAICompatDriver: API error on hop %d: %s", hop, exc)
                        fatal_msg = _fatal_api_error_message(exc)
                        if fatal_msg is not None:
                            return fatal_msg
                        return None

                    if cancel_event is not None and cancel_event.is_set():
                        return None

                    if not tool_calls:
                        return _sanitize_assistant_text(
                            response_text,
                            agent_name=agent_name,
                            session_id=session_id,
                            base_dir=base_dir,
                        ) if response_text else None

                    if hop == max_tool_hops:
                        _logger.warning("OpenAICompatDriver: max tool hops (%d) reached", max_tool_hops)
                        if on_tool_abort is not None:
                            on_tool_abort("max_tool_hops")
                        return _sanitize_assistant_text(
                            response_text,
                            agent_name=agent_name,
                            session_id=session_id,
                            base_dir=base_dir,
                        ) if response_text else "Limite de chamadas de ferramenta atingido."

                    # Adiciona turno do assistente com os tool calls
                    assistant_msg: dict = {
                        "role": "assistant",
                        "content": response_text or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                    messages.append(assistant_msg)

                    # Executa cada ferramenta e adiciona os resultados
                    for tc in tool_calls:
                        if cancel_event is not None and cancel_event.is_set():
                            return None
                        if on_tool_call is not None:
                            on_tool_call(tc["name"], tc["arguments"])
                        if cancel_event is not None and cancel_event.is_set():
                            return None
                        result = self._execute_tool(
                            tc,
                            tool_executor,
                            agent_name=agent_name,
                            parent_agent=parent_agent,
                            progress_callback=progress_callback,
                        )
                        if cancel_event is not None and cancel_event.is_set():
                            return None
                        _logger.info(
                            "OpenAICompatDriver: tool=%s ok=%s hop=%d",
                            tc["name"], result.ok, hop,
                        )
                        if on_tool_result is not None:
                            on_tool_result(result)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(
                                result.to_prompt_payload(_MAX_TOOL_RESULT_CHARS),
                                ensure_ascii=False,
                            ),
                        })
                        if self._is_invalid_tool_result(result):
                            invalid_signature = _invalid_tool_signature(result)
                            if last_invalid_signature == invalid_signature:
                                consecutive_invalid_signature_count += 1
                            else:
                                last_invalid_signature = invalid_signature
                                consecutive_invalid_signature_count = 1
                            if consecutive_invalid_signature_count >= max_consecutive_invalid_signatures:
                                _logger.warning(
                                    "OpenAICompatDriver: repeated invalid policy error_type=%s tool=%s hop=%d count=%d/%d",
                                    result.error_type,
                                    tc["name"],
                                    hop,
                                    consecutive_invalid_signature_count,
                                    max_consecutive_invalid_signatures,
                                )
                                if on_tool_abort is not None:
                                    on_tool_abort("invalid_tool_loop")
                                return "Falha: loop de ferramenta inválida detectado."
                        else:
                            last_invalid_signature = None
                            consecutive_invalid_signature_count = 0
                    messages = _prune_tool_loop_messages(messages)

                return None
            finally:
                # Reseta approve-all (não-permanente) ao fim do ciclo de tool hops.
                if tool_executor is not None:
                    tool_executor.reset_approval_cycle()

    def _is_invalid_tool_result(self, result: ToolResult) -> bool:
        """Indica se o resultado representa uso de ferramenta fora do contrato conhecido."""
        return (not result.ok) and result.error_type == "policy"

    def _chat(self, messages: list[dict], tools: list[dict], cancel_event=None, on_text_chunk=None) -> tuple[str, list[dict]]:
        """Despacha para o modo correto conforme presença de ferramentas.
        extra_body é repassado para permitir que o profile controle parâmetros como 'thinking'."""
        # Cancelamento cooperativo: se já foi solicitado, não iniciar nova interação
        if cancel_event is not None and cancel_event.is_set():
            return "", []
        if tools:
            return self._chat_with_tools(
                messages,
                tools,
                cancel_event=cancel_event,
                on_text_chunk=on_text_chunk,
            )
        return self._chat_streaming(messages, cancel_event=cancel_event, on_text_chunk=on_text_chunk)

    def _chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        cancel_event=None,
        on_text_chunk=None,
    ) -> tuple[str, list[dict]]:
        """
        Chamada não-streaming quando há ferramentas.

        O modo não-streaming permite receber message.tool_calls estruturados
        dos endpoints compatíveis com OpenAI que suportam tool calling nativo.
        A resposta chega de uma vez (sem streaming real), mas o texto bruto
        (incluindo blocos <think>) ainda é repassado a on_text_chunk para que
        o raciocínio apareça no feed do agente, como ocorre no modo streaming.
        """
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.0,
            tools=tools,
            tool_choice="auto",
            **( {"extra_body": self.extra_body} if self.extra_body else {} ),
            stream=False,
        )
        if cancel_event is not None and cancel_event.is_set():
            return "", []
        if not response.choices:
            raise ValueError(
                f"API retornou choices vazio ou None (model={self.model!r}): {response!r}"
            )
        choice = response.choices[0]
        reasoning = getattr(choice.message, "reasoning", None) or getattr(choice.message, "reasoning_content", None)
        if reasoning and on_text_chunk is not None and not (
            cancel_event is not None and cancel_event.is_set()
        ):
            on_text_chunk(f"<think>{reasoning}</think>")
        text = (choice.message.content or "").strip()
        if text and on_text_chunk is not None and not (
            cancel_event is not None and cancel_event.is_set()
        ):
            on_text_chunk(text)
        tool_calls: list[dict] = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    _logger.warning(
                        "OpenAICompatDriver: falha ao parsear argumentos da tool '%s': %r",
                        tc.function.name, tc.function.arguments,
                    )
                    arguments = {}
                tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": arguments})


        return _sanitize_assistant_text(text), tool_calls

    def _chat_streaming(self, messages: list[dict], cancel_event=None, on_text_chunk=None) -> tuple[str, list[dict]]:
        """
        Chamada streaming para respostas de texto puro (sem ferramentas).
        Evita timeout em respostas longas sem bloquear a coleta.
        """
        stream = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
        )
        text = ""
        reasoning_open = False
        for chunk in stream:
            if cancel_event is not None and cancel_event.is_set():
                break
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
            diff = normalize_stream_diff(getattr(delta, "diff", None))

            if reasoning:
                if on_text_chunk is not None:
                    piece = f"<think>{reasoning}" if not reasoning_open else reasoning
                    on_text_chunk(piece)
                reasoning_open = True
                continue

            if reasoning_open and (content or diff):
                reasoning_open = False
                if on_text_chunk is not None:
                    on_text_chunk("</think>")

            if diff:
                text = apply_stream_diff(text, diff)
                if on_text_chunk is not None:
                    on_text_chunk({"text": content or "", "diff": diff})
                continue

            if content:
                text += content
                if on_text_chunk is not None:
                    on_text_chunk(content)

        if reasoning_open and on_text_chunk is not None:
            on_text_chunk("</think>")
        return text.strip(), []

    def _execute_tool(
        self,
        tc: dict,
        tool_executor,
        agent_name: str | None = None,
        parent_agent: str | None = None,
        progress_callback=None,
    ) -> ToolResult:
        """Executa um tool call via ToolExecutor."""
        metadata: dict = {}
        if agent_name:
            metadata["calling_agent"] = agent_name
        trusted_context = TrustedToolExecutionContext(
            agent_name=agent_name,
            parent_agent=parent_agent,
            transport="openai_compat",
            server_origin="openai_compat_driver",
        )
        metadata["trusted_context"] = trusted_context
        tool_call = ToolCall(
            name=tc["name"],
            arguments=tc["arguments"],
            call_id=tc["id"],
            metadata=metadata,
        )
        try:
            return tool_executor.execute(tool_call, progress_callback=progress_callback)
        except Exception as exc:
            _logger.error("OpenAICompatDriver: tool execution failed for '%s': %s", tc["name"], exc)
            return ToolResult(ok=False, tool_name=tc["name"], error=str(exc))
