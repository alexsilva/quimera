"""Driver `claudecloud`: Anthropic Messages API via OAuth do Claude Code.

Fala diretamente com ``https://api.anthropic.com/v1/messages`` usando os
tokens OAuth da subscription (mesma conta do `claude login`), sem executar o
binário ``claude``. O loop de tool calling é herdado de
:class:`OpenAICompatDriver`, então o modelo enxerga exclusivamente as
ferramentas do Quimera (ToolExecutor) — nenhuma ferramenta embutida do
Claude Code é exposta.

O histórico interno permanece no formato chat (OpenAI) e é convertido para o
formato Anthropic a cada request, como o CodexCloudDriver faz com Responses.
"""
from __future__ import annotations

import json
import logging
from collections import OrderedDict
from typing import Optional

import httpx

from ..claude_auth import ClaudeAuthError, ClaudeCloudAuth
from .openai_compat import (
    DEFAULT_MAX_CONNECTIONS,
    FatalAPIError,
    OpenAICompatDriver,
    ToolLoopBudget,
    TransientAPIError,
    _parse_retry_after,
    _parse_tool_arguments,
)

_logger = logging.getLogger(__name__)

DEFAULT_CLAUDE_CLOUD_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"

# Teto de saída por turno, compartilhado com o thinking adaptativo — um turno
# de raciocínio pesado consome o mesmo budget da resposta. 32k cabe em todos
# os modelos suportados (menor teto atual: opus-4-1 = 32k); override via
# extra_body["max_tokens"].
_DEFAULT_MAX_TOKENS = 32000

# Máximo de turnos assistant com blocos originais retidos para replay.
# O contrato da Messages API pede os blocos thinking (com signature)
# preservados quando o assistant do turno tem tool_use; sem eles o modelo
# perde o raciocínio entre hops e o request passa a depender de leniência
# não documentada do backend.
_MAX_TURN_BLOCKS = 256

# A Anthropic só aceita tokens OAuth de subscription em requisições que se
# identificam como Claude Code: o PRIMEIRO bloco de system precisa ser
# exatamente este texto. Sem ele o backend devolve um 429 disfarçado
# ("rate_limit_error: Error", sem headers de rate limit) em vez de 403.
CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

# Famílias que só aceitam thinking com budget_tokens; `adaptive` dá 400 nelas,
# então o default de thinking do driver não se aplica a elas.
_PRE_ADAPTIVE_MODELS = (
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-opus-4-5",
    "claude-3",
)

_TRANSIENT_HTTP_STATUS = {408, 409, 429, 529}
_FATAL_AUTH_MESSAGE = (
    "Login do Claude Code expirado ou revogado (HTTP 401). "
    "Rode `claude login` para reautenticar."
)


def _raise_http_error(status: int, detail: str, headers=None) -> None:
    """Converte erros HTTP da Messages API em falhas retryable ou fatais."""
    snippet = (detail or "").strip()[:500]
    if status == 401:
        raise FatalAPIError(f"claudecloud: {_FATAL_AUTH_MESSAGE}")
    if status == 429:
        raise TransientAPIError(
            f"claudecloud: rate limit da Anthropic (HTTP 429): {snippet}",
            rate_limited=True,
            retry_after=_parse_retry_after(
                (headers.get("retry-after") if headers else None)
            ) if headers else None,
        )
    if status in _TRANSIENT_HTTP_STATUS or status >= 500:
        raise TransientAPIError(
            f"claudecloud: erro transitório da Anthropic (HTTP {status}): {snippet}"
        )
    raise FatalAPIError(
        f"claudecloud: requisição rejeitada pela Anthropic (HTTP {status}): {snippet}",
        user_message=f"A Anthropic rejeitou a requisição (HTTP {status}).",
    )


def _text_of(content) -> str:
    """Extrai texto de content chat-style (str ou lista de partes)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text"):
                parts.append(str(part["text"]))
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _image_block_from_url(url: str) -> dict | None:
    """Converte data URL em bloco image da Anthropic; URLs remotas são ignoradas."""
    if not url or not url.startswith("data:"):
        return None
    try:
        header, data = url.split(",", 1)
        media_type = header.split(";")[0].split(":")[1]
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    except (IndexError, ValueError):
        return None


def _mark_conversation_cache_breakpoint(messages: list[dict]) -> None:
    """Marca o último bloco da conversa com cache_control (breakpoint móvel).

    A cada hop do loop de tools o prefixo até o hop anterior vira cache hit;
    sem isso todo o histórico é reprocessado a preço cheio contra a cota da
    subscription.
    """
    for message in reversed(messages):
        content = message.get("content")
        if isinstance(content, str):
            if not content:
                continue
            message["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }]
            return
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            content[-1]["cache_control"] = {"type": "ephemeral"}
            return


def _chat_tools_to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """Converte schemas de tools do formato chat para o formato Anthropic."""
    converted: list[dict] = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        converted.append({
            "name": function["name"],
            "description": function.get("description") or "",
            "input_schema": function.get("parameters") or {
                "type": "object", "properties": {},
            },
        })
    return converted


class ClaudeCloudDriver(OpenAICompatDriver):
    """Driver da Anthropic Messages API (conta do Claude Code) com tools do Quimera.

    Herda de :class:`OpenAICompatDriver` para reutilizar o loop de tool
    calling, orçamentos de hops e integração com o AgentClient; substitui a
    camada de transporte por chamadas SSE à Messages API com OAuth.
    """

    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_CLAUDE_CLOUD_BASE_URL,
        timeout: Optional[int] = None,
        tool_use_reliability: str = "medium",
        extra_body: Optional[dict] = None,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_model_requests: int | None = None,
        auth: ClaudeCloudAuth | None = None,
        http_client: httpx.Client | None = None,
        loop_budget: ToolLoopBudget | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        runtime_secrets=None,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key="claudecloud",
            timeout=timeout,
            tool_use_reliability=tool_use_reliability,
            extra_body=extra_body,
            max_connections=max_connections,
            max_model_requests=max_model_requests,
            loop_budget=loop_budget,
        )
        self._messages_url = base_url.rstrip("/") + "/v1/messages"
        self._models_url = base_url.rstrip("/") + "/v1/models"
        self._auth = auth or ClaudeCloudAuth(runtime_secrets=runtime_secrets)
        # Alias curto ("sonnet") resolvido para o model id real via /v1/models
        # na primeira requisição; a Messages API rejeita aliases com 404.
        self._resolved_model: str | None = (
            self.model if str(self.model or "").startswith("claude-") else None
        )
        read_timeout = float(timeout) if timeout else 300.0
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(connect=15.0, read=read_timeout, write=30.0, pool=30.0),
        )
        self._owns_http = http_client is None
        self._max_tokens = int(max_tokens) if max_tokens else _DEFAULT_MAX_TOKENS
        # id do primeiro tool_use do turno -> blocos originais do assistant
        # (thinking/text/tool_use, na ordem do stream) para replay nos hops.
        self._turn_blocks: OrderedDict[str, tuple[dict, ...]] = OrderedDict()

    def close(self) -> None:
        """Fecha o cliente HTTP próprio além dos recursos herdados."""
        super().close()
        if self._owns_http:
            try:
                self._http.close()
            except Exception:
                _logger.exception("claudecloud: falha ao fechar cliente HTTP")

    # ------------------------------------------------------------------
    # Conversão chat -> Anthropic
    # ------------------------------------------------------------------

    def _build_anthropic_payload(self, messages: list[dict], tools: list[dict]) -> dict:
        """Monta o corpo da Messages API a partir do histórico chat."""
        system_parts: list[str] = []
        anthropic_messages: list[dict] = []
        # Resultados tool vindos do loop (role=tool) são agrupados num único
        # user message com vários blocos tool_result, como a API exige.
        pending_results: list[dict] = []

        def _flush_results() -> None:
            if pending_results:
                anthropic_messages.append({
                    "role": "user",
                    "content": list(pending_results),
                })
                pending_results.clear()

        for message in messages:
            role = message.get("role")
            if role == "system":
                text = _text_of(message.get("content")).strip()
                if text:
                    system_parts.append(text)
                continue
            if role == "user":
                _flush_results()
                content = message.get("content")
                if isinstance(content, str):
                    anthropic_messages.append({"role": "user", "content": content})
                elif isinstance(content, list):
                    blocks: list[dict] = []
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "text" and part.get("text"):
                            blocks.append({"type": "text", "text": str(part["text"])})
                        elif part.get("type") == "image_url":
                            image = part.get("image_url")
                            url = image.get("url") if isinstance(image, dict) else image
                            block = _image_block_from_url(str(url or ""))
                            if block is not None:
                                blocks.append(block)
                    anthropic_messages.append({
                        "role": "user",
                        "content": blocks or _text_of(content),
                    })
                else:
                    anthropic_messages.append({
                        "role": "user", "content": str(content or ""),
                    })
                continue
            if role == "assistant":
                _flush_results()
                replayed = self._replay_turn_blocks(message.get("tool_calls") or [])
                if replayed is not None:
                    anthropic_messages.append({"role": "assistant", "content": replayed})
                    continue
                blocks = []
                text = _text_of(message.get("content")).strip()
                if text:
                    blocks.append({"type": "text", "text": text})
                for tool_call in message.get("tool_calls") or []:
                    function = tool_call.get("function") or {}
                    raw_args = function.get("arguments") or "{}"
                    try:
                        parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except (json.JSONDecodeError, TypeError):
                        parsed = {}
                    if not isinstance(parsed, dict):
                        parsed = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tool_call.get("id"),
                        "name": function.get("name"),
                        "input": parsed,
                    })
                if blocks:
                    anthropic_messages.append({"role": "assistant", "content": blocks})
                continue
            if role == "tool":
                try:
                    payload = json.loads(str(message.get("content") or "{}"))
                except (json.JSONDecodeError, TypeError):
                    payload = {"text": str(message.get("content") or "")}
                if isinstance(payload, dict) and "text" in payload and len(payload) == 1:
                    result_content = str(payload["text"])
                else:
                    result_content = json.dumps(payload, ensure_ascii=False)
                pending_results.append({
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id"),
                    "content": result_content,
                })
        _flush_results()

        extra = dict(self.extra_body) if self.extra_body else {}
        max_tokens = extra.pop("max_tokens", self._max_tokens)
        body: dict = {
            "model": self._resolved_model or self.model,
            "max_tokens": int(max_tokens),
            "stream": True,
        }
        system_blocks: list[dict] = [{"type": "text", "text": CLAUDE_CODE_IDENTITY}]
        if system_parts:
            system_blocks.append({"type": "text", "text": "\n\n".join(system_parts)})
        # Breakpoint estável de cache: cobre tools + system entre os hops do
        # loop de tools (render: tools -> system -> messages).
        system_blocks[-1]["cache_control"] = {"type": "ephemeral"}
        body["system"] = system_blocks
        _mark_conversation_cache_breakpoint(anthropic_messages)
        body["messages"] = anthropic_messages
        anthropic_tools = _chat_tools_to_anthropic_tools(tools)
        if anthropic_tools:
            body["tools"] = anthropic_tools
        body.update(extra)
        # Sem display o thinking chega com deltas vazios ("omitted") e o feed
        # de raciocínio do Quimera fica mudo; extra_body tem precedência.
        if not str(body["model"]).startswith(_PRE_ADAPTIVE_MODELS):
            body.setdefault("thinking", {"type": "adaptive", "display": "summarized"})
        return body

    def _remember_turn_blocks(self, tool_use_id: str, blocks: list[dict]) -> None:
        self._turn_blocks[tool_use_id] = tuple(blocks)
        while len(self._turn_blocks) > _MAX_TURN_BLOCKS:
            self._turn_blocks.popitem(last=False)

    def _replay_turn_blocks(self, tool_calls: list[dict]) -> list[dict] | None:
        """Retorna os blocos originais do turno se ainda casarem com o histórico.

        Turnos com tool_use precisam voltar com os blocos thinking (e suas
        signatures) na posição original; o texto reconstruído do ledger não
        os carrega. Se o harness editou/compactou o turno (ids divergem),
        devolve None e o caminho de reconstrução assume.
        """
        if not tool_calls:
            return None
        first_id = str((tool_calls[0] or {}).get("id") or "")
        remembered = self._turn_blocks.get(first_id)
        if not remembered:
            return None
        remembered_ids = [
            str(block.get("id") or "")
            for block in remembered
            if block.get("type") == "tool_use"
        ]
        current_ids = [str((call or {}).get("id") or "") for call in tool_calls]
        if remembered_ids != current_ids:
            return None
        # Cópia rasa: _mark_conversation_cache_breakpoint anota cache_control
        # no último bloco da conversa e não pode mutar a side-table.
        return [dict(block) for block in remembered]

    # ------------------------------------------------------------------
    # Transporte SSE
    # ------------------------------------------------------------------

    def _request_headers(self, access_token: str) -> dict:
        return {
            "Authorization": f"Bearer {access_token}",
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": ANTHROPIC_OAUTH_BETA,
            "x-app": "cli",
            "user-agent": "quimera-claudecloud/1.0",
            "accept": "text/event-stream",
            "content-type": "application/json",
        }

    def _resolve_model_alias(self, access_token: str) -> None:
        """Resolve alias curto ("sonnet") para o model id mais novo da família."""
        alias = str(self.model or "").strip().lower()
        try:
            response = self._http.get(
                self._models_url,
                headers=self._request_headers(access_token),
                params={"limit": 100},
            )
        except httpx.HTTPError as exc:
            raise TransientAPIError(
                f"claudecloud: falha ao listar modelos para resolver '{alias}': {exc}"
            ) from exc
        if response.status_code != 200:
            _raise_http_error(
                response.status_code,
                response.text,
                response.headers,
            )
        available: list[str] = []
        for entry in response.json().get("data") or []:
            model_id = str(entry.get("id") or "")
            available.append(model_id)
            # A lista vem da mais nova para a mais antiga; o primeiro match
            # da família é o modelo mais recente.
            if model_id == f"claude-{alias}" or model_id.startswith(f"claude-{alias}-"):
                self._resolved_model = model_id
                _logger.info(
                    "claudecloud: alias %r resolvido para %s", self.model, model_id
                )
                return
        raise FatalAPIError(
            f"claudecloud: modelo '{self.model}' não existe na Anthropic. "
            f"Disponíveis: {', '.join(available) or 'nenhum'}.",
            user_message=(
                f"O modelo '{self.model}' não existe; use um alias como 'sonnet' "
                "ou um id completo como 'claude-sonnet-5'."
            ),
        )

    def _messages_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        cancel_event=None,
        on_text_chunk=None,
    ) -> tuple[str, list[dict]]:
        """Executa um turno contra a Messages API, com retry único em 401."""
        body: dict | None = None
        for attempt in range(2):
            try:
                access_token = self._auth.credentials(force_refresh=attempt > 0)
            except ClaudeAuthError as exc:
                raise FatalAPIError(
                    f"claudecloud: {exc}",
                    cause=exc,
                    user_message=(
                        "Não foi possível autenticar o Claude Cloud. "
                        "Refaça o login com `claude login`."
                    ),
                ) from exc
            if self._resolved_model is None:
                self._resolve_model_alias(access_token)
            if body is None:
                body = self._build_anthropic_payload(messages, tools)
            headers = self._request_headers(access_token)
            try:
                with self._http.stream(
                    "POST", self._messages_url, headers=headers, json=body
                ) as response:
                    if response.status_code == 401 and attempt == 0:
                        response.read()
                        continue
                    if response.status_code != 200:
                        try:
                            detail = response.read().decode("utf-8", errors="replace")
                        except httpx.HTTPError:
                            detail = ""
                        _raise_http_error(response.status_code, detail, response.headers)
                    return self._consume_stream(
                        response,
                        cancel_event=cancel_event,
                        on_text_chunk=on_text_chunk,
                    )
            except httpx.TimeoutException as exc:
                raise TransientAPIError(
                    f"claudecloud: timeout da Anthropic: {exc}",
                    user_message="O Claude Cloud demorou além do limite e foi encerrado.",
                ) from exc
            except httpx.HTTPError as exc:
                raise TransientAPIError(f"claudecloud: falha de rede: {exc}") from exc
        raise FatalAPIError(
            "claudecloud: Anthropic recusou o token mesmo após refresh. "
            "Rode `claude login` para reautenticar."
        )

    def _consume_stream(
        self,
        response: httpx.Response,
        cancel_event=None,
        on_text_chunk=None,
    ) -> tuple[str, list[dict]]:
        """Consome os eventos SSE de um turno e retorna (texto, tool_calls)."""
        text = ""
        # index -> {id, name, json}
        tool_buffers: dict[int, dict] = {}
        # index -> bloco em construção na forma final da Messages API; usado
        # para reter o turno (thinking volta com signature no replay dos hops).
        blocks: dict[int, dict] = {}
        thinking_open = False
        stop_reason: str | None = None
        stop_details: dict = {}
        completed = False

        def _emit(piece: str) -> None:
            if on_text_chunk is not None and piece:
                on_text_chunk(piece)

        def _close_thinking() -> None:
            nonlocal thinking_open
            if thinking_open:
                _emit("</think>")
                thinking_open = False

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
            etype = event.get("type")

            if etype == "content_block_start":
                index = int(event.get("index", 0))
                block = event.get("content_block") or {}
                btype = str(block.get("type") or "")
                if btype == "text":
                    blocks[index] = {"type": "text", "text": ""}
                elif btype == "thinking":
                    blocks[index] = {"type": "thinking", "thinking": "", "signature": ""}
                elif btype == "redacted_thinking":
                    # Chega completo no start (payload criptografado em `data`).
                    blocks[index] = dict(block)
                elif btype == "tool_use":
                    tool_buffers[index] = {
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "json": "",
                    }
                    blocks[index] = {
                        "type": "tool_use",
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "input": {},
                    }
                continue

            if etype == "content_block_delta":
                index = int(event.get("index", 0))
                delta = event.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    piece = str(delta.get("text") or "")
                    if piece:
                        _close_thinking()
                        text += piece
                        _emit(piece)
                        block = blocks.get(index)
                        if block is not None and block.get("type") == "text":
                            block["text"] += piece
                elif dtype == "input_json_delta":
                    buf = tool_buffers.get(index)
                    if buf is not None:
                        buf["json"] += str(delta.get("partial_json") or "")
                elif dtype == "thinking_delta":
                    piece = str(delta.get("thinking") or "")
                    if piece:
                        block = blocks.get(index)
                        if block is not None and block.get("type") == "thinking":
                            block["thinking"] += piece
                        if not thinking_open:
                            _emit("<think>" + piece)
                            thinking_open = True
                        else:
                            _emit(piece)
                elif dtype == "signature_delta":
                    # Assinatura criptográfica do bloco thinking: obrigatória
                    # no replay, nunca exibida.
                    block = blocks.get(index)
                    if block is not None and block.get("type") == "thinking":
                        block["signature"] += str(delta.get("signature") or "")
                continue

            if etype == "content_block_stop":
                _close_thinking()
                continue

            if etype == "message_delta":
                delta = event.get("delta") or {}
                if delta.get("stop_reason"):
                    stop_reason = str(delta["stop_reason"])
                details = delta.get("stop_details")
                if isinstance(details, dict):
                    stop_details = details
                continue

            if etype == "message_stop":
                completed = True
                continue

            if etype == "error":
                _close_thinking()
                error = event.get("error") or {}
                etype_inner = str(error.get("type") or "")
                message = str(error.get("message") or "erro no stream")
                if etype_inner in {"rate_limit_error", "overloaded_error", "api_error"}:
                    raise TransientAPIError(
                        f"claudecloud: {message}",
                        rate_limited=(etype_inner == "rate_limit_error"),
                    )
                raise FatalAPIError(
                    f"claudecloud: stream falhou ({etype_inner}): {message}",
                    user_message="O Claude Cloud interrompeu a resposta.",
                )

        _close_thinking()

        cancelled = cancel_event is not None and cancel_event.is_set()
        if not completed and not cancelled and stop_reason is None:
            raise TransientAPIError(
                "claudecloud: stream encerrado sem evento terminal message_stop"
            )

        if stop_reason == "refusal":
            explanation = str(stop_details.get("explanation") or "").strip()
            category = str(stop_details.get("category") or "").strip()
            detail = " — ".join(part for part in (category, explanation) if part)
            raise FatalAPIError(
                "claudecloud: modelo recusou a resposta (stop_reason=refusal"
                + (f": {detail}" if detail else "") + ")",
                user_message=(
                    "O Claude recusou esta solicitação por política de segurança"
                    + (f" ({explanation})." if explanation else ".")
                ),
            )

        tool_calls: list[dict] = []
        for index in sorted(tool_buffers):
            buf = tool_buffers[index]
            if not buf.get("id") or not buf.get("name"):
                continue
            raw_args = buf.get("json") or "{}"
            arguments, argument_error = _parse_tool_arguments(buf["name"], raw_args)
            tool_calls.append({
                "id": buf["id"],
                "name": buf["name"],
                "arguments": arguments,
                "raw_arguments": raw_args,
                "argument_error": argument_error,
            })
            block = blocks.get(index)
            if block is not None and isinstance(arguments, dict):
                block["input"] = arguments

        if stop_reason == "max_tokens" and not cancelled:
            _logger.warning(
                "claudecloud: resposta truncada por max_tokens model=%s max_tokens=%d",
                self.model, self._max_tokens,
            )
            notice = "\n\n[claudecloud: resposta truncada ao atingir o limite de max_tokens]"
            text += notice
            _emit(notice)

        if tool_calls:
            retained: list[dict] = []
            has_thinking = False
            for index in sorted(blocks):
                block = blocks[index]
                btype = block.get("type")
                if btype == "text" and not block.get("text"):
                    continue
                if btype == "thinking":
                    # Sem signature (ex.: stream truncado) o bloco é rejeitado
                    # no replay; melhor cair na reconstrução sem thinking.
                    if not block.get("signature"):
                        continue
                    has_thinking = True
                elif btype == "redacted_thinking":
                    has_thinking = True
                retained.append(block)
            # Só vale reter quando há thinking a preservar; sem ele a
            # reconstrução a partir do ledger é equivalente.
            if has_thinking:
                self._remember_turn_blocks(str(tool_calls[0]["id"]), retained)

        _logger.info(
            "claudecloud: turno concluído model=%s stop_reason=%s tools=%d chars=%d",
            self.model, stop_reason, len(tool_calls), len(text),
        )
        return text, tool_calls

    # ------------------------------------------------------------------
    # Overrides do transporte herdado
    # ------------------------------------------------------------------

    def _chat_streaming(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        cancel_event=None,
        on_text_chunk=None,
    ) -> tuple[str, list[dict]]:
        return self._messages_turn(
            messages, tools or [], cancel_event=cancel_event, on_text_chunk=on_text_chunk
        )
