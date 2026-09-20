"""Motor único para backends cloud autenticados por OAuth.

Os backends registrados conhecem apenas suas credenciais, payloads, headers,
erros e eventos de stream. Esta classe concentra o harness de tools, cliente
HTTP, concorrência, retry de 401, cancelamento e lifecycle do transporte.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol

import httpx

from .openai_compat import (
    DEFAULT_MAX_CONNECTIONS,
    FatalAPIError,
    ToolCallingDriver,
    ToolLoopBudget,
    TransientAPIError,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloudRequest:
    """Request HTTP normalizado produzido por um backend cloud."""

    url: str
    headers: dict[str, str]
    body: dict


class CloudBackend(Protocol):
    """Fronteira deliberadamente pequena entre o motor e cada protocolo."""

    id: str
    display_name: str
    default_base_url: str
    default_loop_budget: ToolLoopBudget | None

    def credentials(self, *, force_refresh: bool = False) -> Any: ...
    def prepare(self, http_client: httpx.Client, credentials: Any) -> None: ...
    def build_request(
        self,
        messages: list[dict],
        tools: list[dict],
        credentials: Any,
    ) -> CloudRequest: ...
    def raise_for_status(self, status: int, detail: str, headers: httpx.Headers) -> None: ...
    def consume_stream(
        self,
        response: httpx.Response,
        *,
        cancel_event=None,
        on_text_chunk=None,
    ) -> tuple[str, list[dict]]: ...
    def unauthorized_error(self) -> FatalAPIError: ...
    def timeout_error(self, exc: httpx.TimeoutException) -> TransientAPIError: ...
    def network_error(self, exc: httpx.HTTPError) -> TransientAPIError: ...


CloudBackendFactory = Callable[..., CloudBackend]
_BACKENDS: dict[str, CloudBackendFactory] = {}
_BUILTINS_LOADED = False


def register_cloud_backend(provider: str, factory: CloudBackendFactory) -> None:
    """Registra um codec/backend sem alterar consumidores do driver."""
    normalized = str(provider or "").strip().lower()
    if not normalized:
        raise ValueError("provider cloud vazio")
    _BACKENDS[normalized] = factory


def _load_builtin_backends() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    # Os imports registram seus backends. São tardios para evitar ciclos com
    # os aliases de compatibilidade CodexCloudDriver/ClaudeCloudDriver.
    from . import codexcloud as _codexcloud  # noqa: F401
    from . import claudecloud as _claudecloud  # noqa: F401

    _BUILTINS_LOADED = True


def cloud_backend_ids() -> tuple[str, ...]:
    """IDs persistíveis dos backends cloud disponíveis."""
    _load_builtin_backends()
    return tuple(
        provider
        for provider, _factory in sorted(
            _BACKENDS.items(),
            key=lambda item: (getattr(item[1], "order", 100), item[0]),
        )
    )


def cloud_provider_options() -> tuple[tuple[str, str], ...]:
    """Opções de UI derivadas do mesmo registro usado pela factory."""
    _load_builtin_backends()
    return tuple(
        (str(getattr(factory, "display_name", provider)), provider)
        for provider, factory in sorted(
            _BACKENDS.items(),
            key=lambda item: (getattr(item[1], "order", 100), item[0]),
        )
    )


def is_cloud_provider(provider: str) -> bool:
    _load_builtin_backends()
    return str(provider or "").strip().lower() in _BACKENDS


def iter_sse_events(response: httpx.Response, cancel_event=None) -> Iterator[dict]:
    """Decodifica o envelope SSE comum e ignora frames sem JSON."""
    for line in response.iter_lines():
        if cancel_event is not None and cancel_event.is_set():
            break
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


class CloudDriver(ToolCallingDriver):
    """Execução cloud única, parametrizada por um backend registrado."""

    def __init__(
        self,
        provider: str,
        model: str,
        base_url: str | None = None,
        timeout: int | float | None = None,
        tool_use_reliability: str = "medium",
        extra_body: dict | None = None,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_model_requests: int | None = None,
        http_client: httpx.Client | None = None,
        loop_budget: ToolLoopBudget | None = None,
        context_window: int | None = None,
        context_reserve_tokens: int | None = None,
        runtime_secrets=None,
        **backend_options,
    ) -> None:
        _load_builtin_backends()
        normalized = str(provider or "").strip().lower()
        factory = _BACKENDS.get(normalized)
        if factory is None:
            available = ", ".join(_BACKENDS) or "nenhum"
            raise ValueError(
                f"backend cloud desconhecido: {provider!r}; disponíveis: {available}"
            )
        resolved_base_url = base_url or str(factory.default_base_url)
        self.provider = normalized
        self._backend = factory(
            model=model,
            base_url=resolved_base_url,
            extra_body=extra_body,
            runtime_secrets=runtime_secrets,
            **backend_options,
        )
        super().__init__(
            model=model,
            base_url=resolved_base_url,
            api_key=f"cloud:{normalized}",
            timeout=timeout,
            tool_use_reliability=tool_use_reliability,
            extra_body=extra_body,
            max_connections=max_connections,
            max_model_requests=max_model_requests,
            loop_budget=loop_budget or self._backend.default_loop_budget,
            context_window=context_window,
            context_reserve_tokens=context_reserve_tokens,
        )
        self._transport_name = "cloud"
        self._server_origin = f"{normalized}_backend"
        read_timeout = float(timeout) if timeout else 300.0
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(
                connect=15.0,
                read=read_timeout,
                write=30.0,
                pool=30.0,
            ),
        )
        self._owns_http = http_client is None

    def __getattr__(self, name: str):
        """Mantém acesso aos helpers específicos durante a migração pública."""
        backend = self.__dict__.get("_backend")
        if backend is not None:
            try:
                return getattr(backend, name)
            except AttributeError:
                pass
        raise AttributeError(name)

    def close(self) -> None:
        """Fecha o cliente HTTP compartilhado de forma idempotente."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        if self._owns_http:
            try:
                self._http.close()
            except Exception:
                _logger.exception("%s: falha ao fechar cliente HTTP", self.provider)

    def _cloud_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        cancel_event=None,
        on_text_chunk=None,
    ) -> tuple[str, list[dict]]:
        """Executa transporte, refresh e tratamento de rede uma única vez."""
        for attempt in range(2):
            credentials = self._backend.credentials(force_refresh=attempt > 0)
            self._backend.prepare(self._http, credentials)
            request = self._backend.build_request(messages, tools, credentials)
            try:
                with self._http.stream(
                    "POST",
                    request.url,
                    headers=request.headers,
                    json=request.body,
                ) as response:
                    if response.status_code == 401:
                        response.read()
                        if attempt == 0:
                            continue
                        raise self._backend.unauthorized_error()
                    if response.status_code != 200:
                        try:
                            detail = response.read().decode("utf-8", errors="replace")
                        except httpx.HTTPError:
                            detail = ""
                        self._backend.raise_for_status(
                            response.status_code,
                            detail,
                            response.headers,
                        )
                    return self._backend.consume_stream(
                        response,
                        cancel_event=cancel_event,
                        on_text_chunk=on_text_chunk,
                    )
            except httpx.TimeoutException as exc:
                raise self._backend.timeout_error(exc) from exc
            except httpx.HTTPError as exc:
                raise self._backend.network_error(exc) from exc
        # O loop só termina por retorno ou exceção; mantém a defesa caso
        # um backend futuro altere esse contrato.
        raise self._backend.unauthorized_error()

    # Compatibilidade para testes/extensões que usavam os nomes específicos.
    def _responses_turn(self, messages, tools, cancel_event=None, on_text_chunk=None):
        if self.provider != "codexcloud":
            raise AttributeError("_responses_turn")
        return self._cloud_turn(messages, tools, cancel_event, on_text_chunk)

    def _messages_turn(self, messages, tools, cancel_event=None, on_text_chunk=None):
        if self.provider != "claudecloud":
            raise AttributeError("_messages_turn")
        return self._cloud_turn(messages, tools, cancel_event, on_text_chunk)

    def _chat_streaming(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        cancel_event=None,
        on_text_chunk=None,
    ) -> tuple[str, list[dict]]:
        return self._cloud_turn(
            messages,
            tools or [],
            cancel_event=cancel_event,
            on_text_chunk=on_text_chunk,
        )


def create_cloud_driver(provider: str, **kwargs) -> CloudDriver:
    """Factory estável usada por AgentClient, REPL e aliases legados."""
    return CloudDriver(provider=provider, **kwargs)
