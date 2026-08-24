"""Driver `codexcloud`: backend Codex da OpenAI via API Responses.

Fala diretamente com ``https://chatgpt.com/backend-api/codex`` usando os
tokens OAuth do Codex CLI (mesma conta), sem executar o binário ``codex``.
O loop de tool calling é herdado de :class:`OpenAICompatDriver`, então o
modelo enxerga exclusivamente as ferramentas do Quimera (ToolExecutor) —
nenhuma ferramenta embutida do Codex CLI é exposta.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import OrderedDict
from typing import Optional

import httpx

from ..codex_auth import CodexAuthError, CodexCloudAuth
from .openai_compat import (
    DEFAULT_MAX_CONNECTIONS,
    FatalAPIError,
    OpenAICompatDriver,
    TransientAPIError,
    _parse_tool_arguments,
    _sanitize_assistant_text,
)

_logger = logging.getLogger(__name__)

DEFAULT_CODEX_CLOUD_BASE_URL = "https://chatgpt.com/backend-api/codex"

# Máximo de itens de reasoning retidos para reenvio junto aos function_calls.
# O backend exige o item de reasoning imediatamente antes do function_call
# correspondente quando `store=false`.
_MAX_REASONING_ITEMS = 256

# Equivalente à seção "Preamble messages" do system prompt do Codex CLI.
# Sem esta instrução, os modelos gpt-5.6-* reduzem o canal commentary
# (exibido como thinking) a títulos curtos em vez de narrar o progresso.
PREAMBLE_INSTRUCTIONS = """## Preamble messages

Before making tool calls, send a brief preamble to the user explaining what \
you're about to do. When sending preamble messages, follow these principles:

- **Logically group related actions**: if you're about to run several related \
commands, describe them together in one preamble rather than sending a \
separate note for each.
- **Keep it concise**: be no more than 1-2 sentences (8-12 words for quick \
updates).
- **Build on prior context**: if this is not your first tool call, use the \
preamble message to connect the dots with what's been done so far and create \
a sense of momentum and clarity for the user to understand your next actions.
- **Keep your tone light, friendly and curious**: add small touches of \
personality in preambles to feel collaborative and engaging.
- Write every preamble in the same language as the conversation."""


def _content_parts_to_text(content) -> str:
    """Extrai texto de content chat-style (str ou lista de partes)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _user_content_to_input_parts(content) -> list[dict]:
    """Converte content de mensagem user para partes da API Responses."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    parts: list[dict] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                parts.append({"type": "input_text", "text": str(part.get("text") or "")})
            elif ptype == "image_url":
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else image
                if url:
                    parts.append({"type": "input_image", "image_url": str(url)})
    return parts or [{"type": "input_text", "text": str(content or "")}]


def _chat_tools_to_responses_tools(tools: list[dict]) -> list[dict]:
    """Converte schemas de tools do formato chat para o formato Responses."""
    converted: list[dict] = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            continue
        converted.append({
            "type": "function",
            "name": function.get("name"),
            "description": function.get("description") or "",
            "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            "strict": False,
        })
    return converted


class CodexCloudDriver(OpenAICompatDriver):
    """Driver do backend Codex (conta ChatGPT do Codex CLI) com tools do Quimera.

    Herda de :class:`OpenAICompatDriver` para reutilizar o loop de tool
    calling, orçamentos de hops e integração com o AgentClient; substitui a
    camada de transporte por chamadas SSE à API Responses do backend Codex.
    """

    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_CODEX_CLOUD_BASE_URL,
        timeout: Optional[int] = None,
        tool_use_reliability: str = "medium",
        extra_body: Optional[dict] = None,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_model_requests: int | None = None,
        auth: CodexCloudAuth | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key="codexcloud",
            timeout=timeout,
            tool_use_reliability=tool_use_reliability,
            extra_body=extra_body,
            max_connections=max_connections,
            max_model_requests=max_model_requests,
        )
        self._responses_url = base_url.rstrip("/") + "/responses"
        self._auth = auth or CodexCloudAuth()
        self._session_id = str(uuid.uuid4())
        read_timeout = float(timeout) if timeout else 300.0
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(connect=15.0, read=read_timeout, write=30.0, pool=30.0),
        )
        self._owns_http = http_client is None
        # call_id -> item de reasoning que precede o function_call no output.
        self._reasoning_items: OrderedDict[str, dict] = OrderedDict()

    def close(self) -> None:
        """Fecha o cliente HTTP próprio além dos recursos herdados."""
        super().close()
        if self._owns_http:
            try:
                self._http.close()
            except Exception:
                _logger.exception("codexcloud: falha ao fechar cliente HTTP")

    # ------------------------------------------------------------------
    # Conversão chat -> Responses
    # ------------------------------------------------------------------

    def _build_responses_payload(self, messages: list[dict], tools: list[dict]) -> dict:
        """Monta o corpo da requisição Responses a partir do histórico chat."""
        instructions: list[str] = []
        input_items: list[dict] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "system":
                text = _content_parts_to_text(content).strip()
                if text:
                    instructions.append(text)
                continue
            if role == "user":
                input_items.append({
                    "type": "message",
                    "role": "user",
                    "content": _user_content_to_input_parts(content),
                })
                continue
            if role == "assistant":
                text = _content_parts_to_text(content).strip()
                if text:
                    input_items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    })
                for tool_call in message.get("tool_calls") or []:
                    function = tool_call.get("function") or {}
                    call_id = tool_call.get("id")
                    reasoning_item = self._reasoning_items.get(str(call_id))
                    if reasoning_item is not None:
                        input_items.append(reasoning_item)
                    input_items.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": function.get("name"),
                        "arguments": function.get("arguments") or "{}",
                    })
                continue
            if role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id"),
                    "output": _content_parts_to_text(content),
                })

        instructions.append(PREAMBLE_INSTRUCTIONS)
        body: dict = {
            "model": self.model,
            "instructions": "\n\n".join(instructions),
            "input": input_items,
            "tools": _chat_tools_to_responses_tools(tools),
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        if self.extra_body:
            body.update(self.extra_body)
        return body

    # ------------------------------------------------------------------
    # Transporte SSE
    # ------------------------------------------------------------------

    def _request_headers(self, access_token: str, account_id: str) -> dict:
        return {
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "accept": "text/event-stream",
            "content-type": "application/json",
            "session_id": self._session_id,
        }

    def _responses_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        cancel_event=None,
        on_text_chunk=None,
    ) -> tuple[str, list[dict]]:
        """Executa um turno contra o backend Codex, com retry único em 401."""
        body = self._build_responses_payload(messages, tools)
        last_unauthorized: str | None = None
        for attempt in range(2):
            try:
                access_token, account_id = self._auth.credentials(force_refresh=attempt > 0)
            except CodexAuthError as exc:
                raise FatalAPIError(f"codexcloud: {exc}", cause=exc) from exc
            headers = self._request_headers(access_token, account_id)
            try:
                with self._http.stream(
                    "POST", self._responses_url, headers=headers, json=body
                ) as response:
                    if response.status_code == 401 and attempt == 0:
                        response.read()
                        last_unauthorized = "HTTP 401 do backend Codex"
                        continue
                    self._raise_for_status(response)
                    return self._consume_stream(
                        response,
                        cancel_event=cancel_event,
                        on_text_chunk=on_text_chunk,
                    )
            except httpx.TimeoutException as exc:
                raise TransientAPIError(f"codexcloud: timeout do backend Codex: {exc}") from exc
            except httpx.HTTPError as exc:
                raise TransientAPIError(f"codexcloud: falha de rede: {exc}") from exc
        raise FatalAPIError(
            "codexcloud: backend Codex recusou o token mesmo após refresh "
            f"({last_unauthorized}). Rode `codex login` para reautenticar."
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status == 200:
            return
        detail = ""
        try:
            detail = response.read().decode("utf-8", errors="replace")[:500]
        except httpx.HTTPError:
            pass
        if status == 429:
            retry_after_raw = response.headers.get("retry-after")
            try:
                retry_after = float(retry_after_raw) if retry_after_raw else None
            except ValueError:
                retry_after = None
            raise TransientAPIError(
                f"codexcloud: rate limit do backend Codex (HTTP 429): {detail}",
                rate_limited=True,
                retry_after=retry_after,
            )
        if status >= 500:
            raise TransientAPIError(
                f"codexcloud: erro do backend Codex (HTTP {status}): {detail}"
            )
        if status == 401:
            raise FatalAPIError(
                "codexcloud: login do Codex CLI expirado ou revogado (HTTP 401). "
                "Rode `codex login` para reautenticar."
            )
        raise FatalAPIError(
            f"codexcloud: requisição rejeitada pelo backend Codex (HTTP {status}): {detail}"
        )

    def _remember_reasoning(self, call_id: str, item: dict) -> None:
        self._reasoning_items[call_id] = item
        while len(self._reasoning_items) > _MAX_REASONING_ITEMS:
            self._reasoning_items.popitem(last=False)

    def _consume_stream(
        self,
        response: httpx.Response,
        cancel_event=None,
        on_text_chunk=None,
    ) -> tuple[str, list[dict]]:
        """Consome os eventos SSE de um turno e retorna (texto, tool_calls)."""
        text = ""
        raw_tool_calls: list[dict] = []
        pending_reasoning: dict | None = None
        reasoning_open = False
        # Itens de mensagem com phase=commentary (narração de progresso dos
        # modelos gpt-5.6-*): exibidos como thinking, fora do texto final.
        commentary_item_ids: set[str] = set()

        def _emit(piece: str) -> None:
            if on_text_chunk is not None and piece:
                on_text_chunk(piece)

        def _close_reasoning() -> None:
            nonlocal reasoning_open
            if reasoning_open:
                _emit("</think>")
                reasoning_open = False

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

            if etype == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "message" and item.get("phase") == "commentary":
                    item_id = str(item.get("id") or "")
                    if item_id:
                        commentary_item_ids.add(item_id)
                continue

            if etype == "response.output_text.delta":
                delta = str(event.get("delta") or "")
                if not delta:
                    continue
                if str(event.get("item_id") or "") in commentary_item_ids:
                    if not reasoning_open:
                        _emit("<think>" + delta)
                        reasoning_open = True
                    else:
                        _emit(delta)
                    continue
                _close_reasoning()
                text += delta
                _emit(delta)
                continue

            if etype in {
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
            }:
                delta = str(event.get("delta") or "")
                if delta:
                    if not reasoning_open:
                        _emit("<think>" + delta)
                        reasoning_open = True
                    else:
                        _emit(delta)
                continue

            if etype == "response.reasoning_summary_part.done":
                # Separa parágrafos entre partes do resumo de raciocínio.
                if reasoning_open:
                    _emit("\n\n")
                continue

            if etype == "response.output_item.done":
                item = event.get("item") or {}
                itype = item.get("type")
                if itype == "message" and str(item.get("id") or "") in commentary_item_ids:
                    _close_reasoning()
                elif itype == "reasoning":
                    pending_reasoning = item
                elif itype == "function_call":
                    call_id = str(item.get("call_id") or "")
                    if pending_reasoning is not None and call_id:
                        self._remember_reasoning(call_id, pending_reasoning)
                        pending_reasoning = None
                    raw_tool_calls.append({
                        "call_id": call_id,
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments"),
                    })
                continue

            if etype == "response.failed":
                _close_reasoning()
                error = (event.get("response") or {}).get("error") or {}
                code = str(error.get("code") or "")
                message = str(error.get("message") or "resposta marcada como failed")
                if "rate_limit" in code:
                    raise TransientAPIError(
                        f"codexcloud: {message}", rate_limited=True
                    )
                raise FatalAPIError(f"codexcloud: backend Codex falhou: {message}")

            if etype == "response.completed":
                usage = (event.get("response") or {}).get("usage") or {}
                _logger.info(
                    "codexcloud: turno concluído model=%s input_tokens=%s output_tokens=%s",
                    self.model,
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                )
                continue

        _close_reasoning()

        tool_calls: list[dict] = []
        for raw in raw_tool_calls:
            arguments, argument_error = _parse_tool_arguments(raw["name"], raw["arguments"])
            tool_calls.append({
                "id": raw["call_id"],
                "name": raw["name"],
                "arguments": arguments,
                "raw_arguments": raw["arguments"],
                "argument_error": argument_error,
            })
        return _sanitize_assistant_text(text), tool_calls

    # ------------------------------------------------------------------
    # Overrides do transporte herdado
    # ------------------------------------------------------------------

    def _chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        cancel_event=None,
        on_text_chunk=None,
    ) -> tuple[str, list[dict]]:
        return self._responses_turn(
            messages, tools, cancel_event=cancel_event, on_text_chunk=on_text_chunk
        )

    def _chat_streaming(
        self,
        messages: list[dict],
        cancel_event=None,
        on_text_chunk=None,
    ) -> tuple[str, list[dict]]:
        return self._responses_turn(
            messages, [], cancel_event=cancel_event, on_text_chunk=on_text_chunk
        )
