"""
Driver para endpoints compatíveis com a API OpenAI: Ollama, OpenRouter, LM Studio, etc.

Suporta tool calling nativo e streaming interno (coleta tokens sem bloquear no timeout).
A exibição da resposta final segue o pipeline normal do app (show_message).
"""
from __future__ import annotations

import json
import hashlib
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
    MAX_MODEL_REQUESTS_BY_RELIABILITY,  # noqa: F401
    MAX_TOOL_HOPS_BY_RELIABILITY,  # noqa: F401
    get_invalid_tool_loop_threshold,
    get_max_model_requests,
    get_max_tool_hops,
)
from .tool_schemas import resolve_tool_schemas
from .prompt_adapter import (
    _build_openai_messages_from_prompt,
    _build_tool_budget_prompt,
    _build_tool_system_prompt,
)
from ..errors import ToolValidationError
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
    from openai import (
        APIConnectionError as _OAIConnectionError,
        APITimeoutError as _OAITimeoutError,
        APIStatusError as _OAIStatusError,
    )
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]
    _OAIAuthError = Exception  # type: ignore[assignment,misc]
    _OAINotFoundError = Exception  # type: ignore[assignment,misc]
    _OAIBadRequestError = Exception  # type: ignore[assignment,misc]
    _OAIRateLimitError = Exception  # type: ignore[assignment,misc]
    _OAIConnectionError = Exception  # type: ignore[assignment,misc]
    _OAITimeoutError = Exception  # type: ignore[assignment,misc]
    _OAIStatusError = Exception  # type: ignore[assignment,misc]


class TransientAPIError(Exception):
    """Erro transitório da API OpenAI-compatible; seguro para retry com backoff.

    ``rate_limited=True`` indica rate limit explícito (HTTP 429) e ativa
    ``rate_limit_detected`` no AgentClient, usando backoff dedicado.
    ``retry_after`` opcional vem do header ``retry-after`` do backend.
    """

    def __init__(
        self,
        message: str,
        *,
        rate_limited: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.rate_limited = bool(rate_limited)
        self.retry_after = retry_after
        self.retryable = True


class FatalAPIError(Exception):
    """Erro fatal da API OpenAI-compatible.

    Não deve ser retryado nem tratado como resposta válida — o modelo,
    a chave ou a requisição são inválidos e o retry não mudaria o resultado.
    """

    def __init__(self, message: str, *, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause
        self.retryable = False


def _api_retry_after(exc: Exception) -> float | None:
    """Lê o header ``retry-after`` da resposta HTTP quando disponível."""
    headers = getattr(exc, "headers", None)
    if headers is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
    if headers is None:
        return None
    get = getattr(headers, "get", None)
    if not callable(get):
        return None
    raw = get("retry-after") or get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _categorize_api_exception(exc: Exception) -> tuple[str, float | None] | None:
    """Classifica uma exceção do SDK em rate_limit/transient/fatal.

    Retorna ``("rate_limit", retry_after)``, ``("transient", None)``,
    ``("fatal", None)`` ou ``None`` para erros não reconhecidos.
    """
    if _OAIRateLimitError is not Exception and isinstance(exc, _OAIRateLimitError):
        return "rate_limit", _api_retry_after(exc)
    if _OAIAuthError is not Exception and isinstance(exc, _OAIAuthError):
        return "fatal", None
    if _OAINotFoundError is not Exception and isinstance(exc, _OAINotFoundError):
        return "fatal", None
    if _OAIBadRequestError is not Exception and isinstance(exc, _OAIBadRequestError):
        return "fatal", None
    if _OAIConnectionError is not Exception and isinstance(exc, _OAIConnectionError):
        return "transient", None
    if _OAITimeoutError is not Exception and isinstance(exc, _OAITimeoutError):
        return "transient", None
    if _OAIStatusError is not Exception and isinstance(exc, _OAIStatusError):
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and 500 <= status < 600:
            return "transient", None
        return "fatal", None
    return None


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
_BACKEND_SEMAPHORE_LOCK = threading.Lock()
_BACKEND_SEMAPHORES: dict[tuple[str, str], "_BackendSemaphore"] = {}


class _BackendSemaphore(threading.Semaphore):
    """Semáforo compartilhado por backend que só aceita limites mais restritivos."""

    def __init__(self, max_connections: int) -> None:
        normalized = _normalize_max_connections(max_connections)
        super().__init__(normalized)
        self._limit = normalized
        self._release_debt = 0

    def tighten_limit(self, max_connections: int) -> None:
        """Aplica o menor limite visto sem liberar capacidade extra."""
        normalized = _normalize_max_connections(max_connections)
        with self._cond:
            if normalized >= self._limit:
                return
            active_holders = self._limit - self._value + self._release_debt
            self._limit = normalized
            self._release_debt = max(active_holders - normalized, 0)
            self._value = max(normalized - active_holders, 0)
            if self._value > 0:
                self._cond.notify(self._value)

    def release(self, n: int = 1) -> None:
        if n < 1:
            raise ValueError("n must be one or more")
        with self._cond:
            for _ in range(n):
                if self._release_debt > 0:
                    self._release_debt -= 1
                    continue
                self._value += 1
                self._cond.notify()

# Intervalo de polling ao aguardar slot no semáforo global, permitindo
# checar cancel_event periodicamente em vez de bloquear indefinidamente.
_SEMAPHORE_POLL_INTERVAL = 0.1


def _acquire_semaphore_cancelable(semaphore: threading.Semaphore, cancel_event) -> bool:
    """Adquire `semaphore` com espera cancelável via polling.

    Usa `acquire(timeout=...)` em vez de bloquear indefinidamente, permitindo
    checar `cancel_event` periodicamente sem busy-wait. Retorna True se o
    semáforo foi adquirido (chamador deve liberar exatamente uma vez) ou
    False se `cancel_event` foi sinalizado antes de um slot ficar disponível
    (nenhuma aquisição pendente nesse caso).
    """
    while True:
        if semaphore.acquire(timeout=_SEMAPHORE_POLL_INTERVAL):
            return True
        if cancel_event is not None and cancel_event.is_set():
            return False


def _normalize_max_connections(max_connections: int) -> int:
    try:
        value = int(max_connections)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONNECTIONS
    return value if value > 0 else DEFAULT_MAX_CONNECTIONS


def _backend_semaphore_key(base_url: str, api_key: str) -> tuple[str, str]:
    api_key_fingerprint = hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()
    return (
        str(base_url or ""),
        api_key_fingerprint,
    )


def _backend_semaphore(
    base_url: str,
    api_key: str,
    max_connections: int,
) -> _BackendSemaphore:
    max_connections = _normalize_max_connections(max_connections)
    key = _backend_semaphore_key(base_url, api_key)
    with _BACKEND_SEMAPHORE_LOCK:
        semaphore = _BACKEND_SEMAPHORES.get(key)
        if semaphore is None:
            semaphore = _BackendSemaphore(max_connections)
            _BACKEND_SEMAPHORES[key] = semaphore
        else:
            semaphore.tighten_limit(max_connections)
        return semaphore


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
    """Gera assinatura estavel para detectar repeticao do mesmo erro invalido."""
    error_text = re.sub(r"\s+", " ", str(result.error or "").strip().lower())
    if len(error_text) > 256:
        error_text = error_text[:256]
    return result.error_type, result.tool_name, error_text


def _parse_tool_arguments(
    tool_name: str,
    raw_arguments,
) -> tuple[dict, ToolValidationError | None]:
    """Converte argumentos OpenAI sem mascarar JSON invalido como objeto vazio."""
    if raw_arguments in (None, ""):
        return {}, None
    if isinstance(raw_arguments, dict):
        return raw_arguments, None
    try:
        arguments = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError):
        _logger.warning(
            "OpenAICompatDriver: falha ao parsear argumentos da tool '%s': %r",
            tool_name,
            raw_arguments,
        )
        return {}, ToolValidationError(
            f"Argumentos JSON invalidos para a ferramenta '{tool_name}'.",
            field="arguments",
            hint="Envie os argumentos como um objeto JSON valido e tente novamente.",
        )
    if not isinstance(arguments, dict):
        return {}, ToolValidationError(
            f"Argumentos da ferramenta '{tool_name}' devem ser um objeto JSON.",
            field="arguments",
            hint="Envie os argumentos como um objeto JSON valido e tente novamente.",
        )
    return arguments, None


def _tool_arguments_message(tc: dict) -> str:
    """Preserva os argumentos emitidos pelo modelo no turno assistant."""
    if tc.get("argument_error") is not None:
        return str(tc.get("raw_arguments") or "")
    return json.dumps(tc["arguments"], ensure_ascii=False)


def _prune_tool_loop_messages(messages: list[dict]) -> list[dict]:
    """Limita o histórico do loop de tools preservando system/user e a cauda recente.

    Invariant enforcement:
    - Every 'tool' message must have a preceding 'assistant' with matching tool_call_id.
    - Every tool_call in an 'assistant' must have a corresponding 'tool' result.
    - System/user messages at the head are always preserved.
    - Total messages never exceed _MAX_TOOL_LOOP_MESSAGES.
    """
    if len(messages) <= _MAX_TOOL_LOOP_MESSAGES:
        return _clean_message_sequence(messages)

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

    # Build valid (assistant, tool_results) pairs from tail, enforcing invariants.
    pairs: list[tuple[dict, list[dict]]] = []
    index = 0
    while index < len(tail):
        msg = tail[index]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            assistant = msg
            tool_calls = assistant.get("tool_calls", [])
            call_ids = {call.get("id") for call in tool_calls if call.get("id")}
            index += 1
            tool_results: list[dict] = []
            while index < len(tail) and tail[index].get("role") == "tool":
                tool_msg = tail[index]
                tcid = tool_msg.get("tool_call_id")
                if tcid in call_ids:
                    tool_results.append(tool_msg)
                index += 1
            # Keep only tool_calls that have corresponding results.
            matched_calls = [call for call in tool_calls if call.get("id") in {r.get("tool_call_id") for r in tool_results}]
            if matched_calls:
                clean_assistant = dict(assistant)
                clean_assistant["tool_calls"] = matched_calls
                pairs.append((clean_assistant, tool_results))
            # If no matched calls, drop this assistant entirely (no orphan tool_calls).
            continue
        # Orphan tool or non-tool-loop message: skip to enforce invariants.
        index += 1

    # Select most recent pairs that fit within 'available' slots.
    # Each pair contributes 1 (assistant) + N (tool results) messages.
    kept_pairs: list[tuple[dict, list[dict]]] = []
    used = 0
    for assistant, tools in reversed(pairs):
        pair_len = 1 + len(tools)
        if kept_pairs and used + pair_len > available:
            break
        if not kept_pairs and pair_len > available:
            # Boundary case: truncate the oldest kept pair to fit.
            if available >= 2:
                max_tools = available - 1
                kept_tools = tools[-max_tools:]
                kept_ids = {t.get("tool_call_id") for t in kept_tools}
                matched_calls = [call for call in assistant.get("tool_calls", []) if call.get("id") in kept_ids]
                if matched_calls:
                    clean_assistant = dict(assistant)
                    clean_assistant["tool_calls"] = matched_calls
                    kept_pairs.append((clean_assistant, kept_tools))
            break
        kept_pairs.append((assistant, tools))
        used += pair_len

    # Reconstruct tail from kept pairs (oldest first).
    pruned_tail: list[dict] = []
    for assistant, tools in reversed(kept_pairs):
        pruned_tail.append(assistant)
        pruned_tail.extend(tools)

    return head + pruned_tail


def _clean_message_sequence(messages: list[dict]) -> list[dict]:
    """Enforce invariants on a message sequence that is already within the size limit."""
    # First pass: identify valid assistant->tool pairs.
    pairs: list[tuple[dict, list[dict]]] = []
    index = 0
    while index < len(messages):
        msg = messages[index]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            assistant = msg
            tool_calls = assistant.get("tool_calls", [])
            call_ids = {call.get("id") for call in tool_calls if call.get("id")}
            index += 1
            tool_results: list[dict] = []
            while index < len(messages) and messages[index].get("role") == "tool":
                tool_msg = messages[index]
                tcid = tool_msg.get("tool_call_id")
                if tcid in call_ids:
                    tool_results.append(tool_msg)
                index += 1
            matched_calls = [call for call in tool_calls if call.get("id") in {r.get("tool_call_id") for r in tool_results}]
            if matched_calls:
                clean_assistant = dict(assistant)
                clean_assistant["tool_calls"] = matched_calls
                pairs.append((clean_assistant, tool_results))
            continue
        # Preserve system/user and other messages (non-tool-loop).
        index += 1

    # Reconstruct: keep head (system/user) + valid pairs.
    head_end = 0
    for msg in messages:
        if msg.get("role") in {"system", "user"}:
            head_end += 1
            continue
        break
    head = messages[:head_end] if head_end > 0 else messages[:min(2, len(messages))]
    pruned: list[dict] = list(head)
    for assistant, tools in pairs:
        pruned.append(assistant)
        pruned.extend(tools)
    return pruned



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

    Um semáforo global por backend/API key limita o número de chamadas
    concorrentes ao mesmo backend para evitar estouro de rate-limit quando
    múltiplos agentes compartilham provider.
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
        max_model_requests: int | None = None,
    ) -> None:
        """Inicializa uma instância de OpenAICompatDriver.
        extra_body: dicionário opcional mesclado no corpo da requisição (ex: {"thinking": {"type": "enabled"}}).
        max_connections: limite de chamadas concorrentes ao backend (padrão: 4).
        max_model_requests: orçamento de requests por execução; None preserva
            o limite histórico associado a tool_use_reliability."""
        self._semaphore = _backend_semaphore(base_url, api_key, max_connections)
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
        default_request_budget = get_max_model_requests(self.tool_use_reliability)
        try:
            normalized_request_budget = int(max_model_requests)
        except (TypeError, ValueError):
            normalized_request_budget = default_request_budget
        self.max_model_requests = (
            normalized_request_budget
            if normalized_request_budget > 0
            else default_request_budget
        )
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

        # O semáforo é adquirido por _chat apenas durante cada request HTTP.
        # Tools locais e aprovações não consomem slots do backend.
        try:
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
                        max_model_requests=self.max_model_requests,
                        remaining_model_requests=self.max_model_requests,
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
                    if hop >= self.max_model_requests:
                        _logger.warning(
                            "OpenAICompatDriver: max model requests (%d) reached",
                            self.max_model_requests,
                        )
                        if on_tool_abort is not None:
                            on_tool_abort("max_model_requests")
                        return "Limite de chamadas ao modelo atingido."
                    if tool_budget_index is not None:
                        remaining_tool_hops = max(max_tool_hops - hop, 0)
                        remaining_model_requests = max(self.max_model_requests - hop, 0)
                        messages[tool_budget_index]["content"] = _build_tool_budget_prompt(
                            max_tool_hops=max_tool_hops,
                            remaining_tool_hops=remaining_tool_hops,
                            max_model_requests=self.max_model_requests,
                            remaining_model_requests=remaining_model_requests,
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
                        categorization = _categorize_api_exception(exc)
                        if categorization is None:
                            return None
                        category, retry_after = categorization
                        if category == "fatal":
                            raise FatalAPIError(
                                _fatal_api_error_message(exc) or f"Erro fatal da API ({self.model}): {exc}",
                                cause=exc,
                            ) from exc
                        if category in ("rate_limit", "transient"):
                            raise TransientAPIError(
                                f"Erro transitório da API ({self.model}): {exc}",
                                rate_limited=(category == "rate_limit"),
                                retry_after=retry_after,
                            ) from exc
                        return None

                    if cancel_event is not None and cancel_event.is_set():
                        # Streaming pode terminar por cancelamento depois de
                        # já produzir texto válido. Preserve somente essa saída
                        # textual parcial; nunca continue uma sequência parcial
                        # de chamadas de ferramenta.
                        if response_text and not tool_calls:
                            return _sanitize_assistant_text(
                                response_text,
                                agent_name=agent_name,
                                session_id=session_id,
                                base_dir=base_dir,
                            )
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
                                    "arguments": _tool_arguments_message(tc),
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                    messages.append(assistant_msg)

                    # Executa cada ferramenta e adiciona os resultados
                    abort_invalid_loop = False
                    saw_invalid_result = False
                    for tc in tool_calls:
                        if cancel_event is not None and cancel_event.is_set():
                            return None
                        argument_error = tc.get("argument_error")
                        if argument_error is not None:
                            result = ToolResult(
                                ok=False,
                                tool_name=tc["name"],
                                error=argument_error,
                                data={"tool_call_id": tc["id"]},
                            )
                        else:
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
                            saw_invalid_result = True
                            invalid_signature = _invalid_tool_signature(result)
                            if last_invalid_signature == invalid_signature:
                                consecutive_invalid_signature_count += 1
                            else:
                                last_invalid_signature = invalid_signature
                                consecutive_invalid_signature_count = 1
                            if consecutive_invalid_signature_count >= max_consecutive_invalid_signatures:
                                _logger.warning(
                                    "OpenAICompatDriver: repeated invalid tool error_type=%s tool=%s hop=%d count=%d/%d",
                                    result.error_type,
                                    tc["name"],
                                    hop,
                                    consecutive_invalid_signature_count,
                                    max_consecutive_invalid_signatures,
                                )
                                abort_invalid_loop = True
                    if not saw_invalid_result:
                        last_invalid_signature = None
                        consecutive_invalid_signature_count = 0
                    if abort_invalid_loop:
                        if on_tool_abort is not None:
                            on_tool_abort("invalid_tool_loop")
                        return "Falha: loop de ferramenta inválida detectado."
                    messages = _prune_tool_loop_messages(messages)

                return None
            finally:
                # Reseta approve-all (não-permanente) ao fim do ciclo de tool hops.
                if tool_executor is not None:
                    tool_executor.reset_approval_cycle()
        finally:
            _logger.debug("OpenAICompatDriver: run finished model=%s", self.model)

    def _is_invalid_tool_result(self, result: ToolResult) -> bool:
        """Indica se o resultado representa uso de ferramenta fora do contrato conhecido."""
        return (not result.ok) and result.error_type in {"policy", "validation"}

    def _chat(self, messages: list[dict], tools: list[dict], cancel_event=None, on_text_chunk=None) -> tuple[str, list[dict]]:
        """Executa um request ao backend respeitando limite e cancelamento."""
        if not _acquire_semaphore_cancelable(self._semaphore, cancel_event):
            return "", []
        try:
            if cancel_event is not None and cancel_event.is_set():
                return "", []
            if tools:
                return self._chat_with_tools(
                    messages,
                    tools,
                    cancel_event=cancel_event,
                    on_text_chunk=on_text_chunk,
                )
            return self._chat_streaming(
                messages,
                cancel_event=cancel_event,
                on_text_chunk=on_text_chunk,
            )
        finally:
            self._semaphore.release()

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
                raw_arguments = tc.function.arguments
                arguments, argument_error = _parse_tool_arguments(
                    tc.function.name,
                    raw_arguments,
                )
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": arguments,
                    "raw_arguments": raw_arguments,
                    "argument_error": argument_error,
                })


        return _sanitize_assistant_text(text), tool_calls

    def _chat_streaming(self, messages: list[dict], cancel_event=None, on_text_chunk=None) -> tuple[str, list[dict]]:
        """
        Chamada streaming para respostas de texto puro (sem ferramentas).
        Evita timeout em respostas longas sem bloquear a coleta.
        """
        stream = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            **({"extra_body": self.extra_body} if self.extra_body else {}),
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
