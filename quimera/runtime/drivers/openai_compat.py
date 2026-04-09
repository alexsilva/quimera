"""
Driver para endpoints compatíveis com a API OpenAI: Ollama, OpenRouter, LM Studio, etc.

Suporta tool calling nativo e streaming interno (coleta tokens sem bloquear no timeout).
A exibição da resposta final segue o pipeline normal do Quimera (show_message).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from ..models import ToolCall, ToolResult
from .tool_schemas import TOOL_SCHEMAS

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]

_logger = logging.getLogger(__name__)

MAX_TOOL_HOPS = 8

# Remove blocos <think>...</think> ou <thinking>...</thinking> que modelos Qwen3 emitem.
_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL)

# Formato XML de text-tool-calls que alguns modelos emitem quando a API não suporta tool_calls:
# <function=NAME><parameter=KEY>VALUE</tool_call>
_TEXT_FUNC_CALL_RE = re.compile(r"<function=(\w+)>(.*?)</tool_call>", re.DOTALL)
_TEXT_PARAM_RE = re.compile(r"<parameter=(\w+)>")


def _strip_thinking(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def _parse_text_tool_calls(text: str) -> list[dict]:
    """
    Parseia tool calls em formato XML de texto emitidas diretamente no conteúdo.

    Formato detectado (ex: qwen3-coder via Ollama sem suporte nativo a tool_calls):
        <function=NAME><parameter=KEY1>VALUE1<parameter=KEY2>VALUE2</tool_call>
    """
    calls = []
    for match in _TEXT_FUNC_CALL_RE.finditer(text):
        name = match.group(1)
        body = match.group(2)
        parts = _TEXT_PARAM_RE.split(body)
        arguments: dict = {}
        # parts: ["pré", KEY1, "VALUE1", KEY2, "VALUE2", ...]
        it = iter(parts[1:])
        for key in it:
            try:
                raw_value = next(it)
                # remover </parameter> se presente
                value = re.sub(r"</parameter>", "", raw_value).strip()
                arguments[key] = value
            except StopIteration:
                break
        calls.append({
            "id": f"text-tc-{len(calls):04d}",
            "name": name,
            "arguments": arguments,
        })
    return calls


class OpenAICompatDriver:
    """
    Driver para qualquer endpoint compatível com OpenAI.

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
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str = "ollama",
        timeout: Optional[int] = None,
    ) -> None:
        if OpenAI is None:
            raise ImportError(
                "O pacote 'openai' é necessário para usar o driver openai_compat. "
                "Instale com: pip install openai"
            )
        self.model = model
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=float(timeout) if timeout else 120.0,
        )

    def run(
        self,
        prompt: str,
        tool_executor=None,
    ) -> Optional[str]:
        """
        Executa o agente com o prompt dado tratando o loop de tool calling internamente.

        Args:
            prompt: Prompt completo construído pelo PromptBuilder.
            tool_executor: Instância de ToolExecutor para executar tool calls.
                           Se None, o agente responde sem ferramentas.

        Returns:
            Texto final da resposta do modelo, ou None em caso de falha.
        """
        tools = TOOL_SCHEMAS if tool_executor is not None else []
        messages: list[dict] = []
        if tools:
            tool_names = ", ".join(t["function"]["name"] for t in tools)
            messages.append({
                "role": "system",
                "content": (
                    f"Você tem acesso às seguintes ferramentas: {tool_names}. "
                    "Use-as sempre que precisar de informações do sistema ou do projeto. "
                    "Não peça ao usuário para executar comandos manualmente se você pode fazer isso diretamente."
                ),
            })
        messages.append({"role": "user", "content": prompt})

        for hop in range(MAX_TOOL_HOPS + 1):
            try:
                response_text, tool_calls = self._chat(messages, tools)
            except Exception as exc:
                _logger.error("OpenAICompatDriver: API error on hop %d: %s", hop, exc)
                return None

            if not tool_calls:
                return _strip_thinking(response_text) if response_text else None

            if hop == MAX_TOOL_HOPS:
                _logger.warning("OpenAICompatDriver: max tool hops (%d) reached", MAX_TOOL_HOPS)
                return _strip_thinking(response_text) if response_text else "Limite de chamadas de ferramenta atingido."

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
                result = self._execute_tool(tc, tool_executor)
                _logger.info(
                    "OpenAICompatDriver: tool=%s ok=%s hop=%d",
                    tc["name"], result.ok, hop,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result.to_model_payload(), ensure_ascii=False),
                })

        return None

    def _chat(self, messages: list[dict], tools: list[dict]) -> tuple[str, list[dict]]:
        """Despacha para o modo correto conforme presença de ferramentas."""
        if tools:
            return self._chat_with_tools(messages, tools)
        return self._chat_streaming(messages)

    def _chat_with_tools(self, messages: list[dict], tools: list[dict]) -> tuple[str, list[dict]]:
        """
        Chamada não-streaming quando há ferramentas.

        Ollama em modo streaming não popula delta.tool_calls em vários modelos —
        o modelo emite o call como texto. O modo não-streaming retorna
        message.tool_calls estruturado de forma confiável.
        """
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            stream=False,
        )
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
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
        # Fallback: modelo emitiu tool calls em formato XML de texto em vez de usar a API
        if not tool_calls and _TEXT_FUNC_CALL_RE.search(text):
            parsed = _parse_text_tool_calls(text)
            if parsed:
                _logger.debug(
                    "OpenAICompatDriver: %d text-format tool call(s) detectados em content (fallback)",
                    len(parsed),
                )
                clean_text = _TEXT_FUNC_CALL_RE.sub("", text).strip()
                return clean_text, parsed

        return text, tool_calls

    def _chat_streaming(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """
        Chamada streaming para respostas de texto puro (sem ferramentas).
        Evita timeout em respostas longas sem bloquear a coleta.
        """
        stream = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
        )
        text_parts: list[str] = []
        for chunk in stream:
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content
            if content:
                text_parts.append(content)
        return "".join(text_parts).strip(), []

    def _execute_tool(self, tc: dict, tool_executor) -> ToolResult:
        """Executa um tool call via ToolExecutor do Quimera."""
        tool_call = ToolCall(
            name=tc["name"],
            arguments=tc["arguments"],
            call_id=tc["id"],
        )
        try:
            return tool_executor.execute(tool_call)
        except Exception as exc:
            _logger.error("OpenAICompatDriver: tool execution failed for '%s': %s", tc["name"], exc)
            return ToolResult(ok=False, tool_name=tc["name"], error=str(exc))
