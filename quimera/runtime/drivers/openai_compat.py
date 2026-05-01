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

from ..streaming import apply_stream_diff, normalize_stream_diff
from ..tool_hops import (
    DEFAULT_MAX_TOOL_HOPS,
    MAX_TOOL_HOPS_BY_RELIABILITY,
    get_max_tool_hops,
)
from .tool_schemas import resolve_tool_schemas
from ..models import ToolCall, ToolResult

MAX_TOOL_HOPS = DEFAULT_MAX_TOOL_HOPS

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]

_logger = logging.getLogger(__name__)

# Remove blocos <think>...</think> ou <thinking>...</thinking> que modelos Qwen3 emitem.
_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL)
_FUNCTION_RESIDUE_RE = re.compile(r"</?(?:function|tool_call)\b[^>]*>")

# Trunca tool results para evitar explosão de memória no array messages.
_MAX_TOOL_RESULT_CHARS = 4000
_MAX_TOOL_LOOP_MESSAGES = 16


# Formato XML de text-tool-calls que alguns modelos emitem quando a API não suporta tool_calls:
# <function=NAME><parameter=KEY>VALUE</tool_call>
_TEXT_FUNC_CALL_RE = re.compile(r"<function=(\w+)>(.*?)</tool_call>", re.DOTALL)
_TEXT_PARAM_RE = re.compile(r"<parameter=(\w+)>")


def _strip_thinking(text: str) -> str:
    """Remove thinking."""
    return _THINK_RE.sub("", text).strip()


def _sanitize_assistant_text(text: str) -> str:
    """Remove resíduos textuais de tool calling que alguns modelos deixam no content."""
    text = _strip_thinking(text)
    text = _FUNCTION_RESIDUE_RE.sub("", text)
    return text.strip()


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


def _build_tool_system_prompt(tool_names: list[str], workspace_root: str | None) -> str:
    """Monta o system prompt usado no modo com ferramentas."""
    names_csv = ", ".join(tool_names)
    available = set(tool_names)
    workspace_hint = f"Workspace raiz: {workspace_root}. " if workspace_root is not None else ""

    instructions = [
        f"Você tem acesso às seguintes ferramentas: {names_csv}. ",
        workspace_hint,
        "Use apenas ferramentas listadas e disponíveis nesta requisição. ",
        "Quando decidir usar uma ferramenta, use o mecanismo de tool calling da API compatível ou o fallback textual já suportado; ",
        "não invente envelopes JSON intermediários como ",
        "{\"action\":\"execute\",\"tool_name\":\"...\",\"params\":{...}}. ",
        "Não escreva chamadas de ferramenta como texto visível ao usuário; ",
        "use exatamente os nomes de argumentos definidos nos schemas das ferramentas; ",
        "se uma ferramenta retornar erro, ajuste a próxima chamada com base no erro e não repita o mesmo payload inválido; ",
        "não peça ao usuário para executar comandos manualmente se você pode fazer isso diretamente; ",
        "na resposta final, resuma arquivos alterados, evidência de validação e próximo passo; ",
        "nunca exponha tags de tool calling como <function>, </function> ou </tool_call> na resposta final.",
    ]

    discovery_tools = [name for name in ("list_files", "grep_search", "read_file") if name in available]
    if discovery_tools:
        instructions.append(
            f" Protocolo de ferramentas: descubra o alvo antes de editar usando {', '.join(discovery_tools)}; "
        )

    if "apply_patch" in available:
        instructions.append("prefira apply_patch para mudanças parciais em arquivos existentes; ")
        instructions.append(
            "o patch de apply_patch deve usar o formato nativo do Quimera e começar exatamente com "
            "'*** Begin Patch' e terminar exatamente com '*** End Patch'; "
        )
        instructions.append("não use cabeçalhos de diff como '---', '+++' ou 'diff --git' dentro do patch; ")

    if "write_file" in available:
        instructions.append(
            "write_file só deve sobrescrever arquivo existente com replace_existing=true e quando a "
            "reescrita total for realmente necessária; "
        )

    if "read_file" in available:
        instructions.append("por exemplo, read_file usa 'path', não 'file_path'; ")

    has_run_shell = "run_shell" in available
    has_exec_command = "exec_command" in available
    if has_run_shell and has_exec_command:
        instructions.append(
            "para shell, use exatamente 'run_shell' para uma execução simples ou 'exec_command' para sessão "
            "interativa; "
        )
        instructions.append("nunca invente nomes como 'run', 'run_shell_command' ou 'execute_command'; ")
        instructions.append(
            "use run_shell para inspeção ou validação objetiva e exec_command apenas quando precisar de stdin, "
            "polling ou sessão persistente; "
        )
    elif has_run_shell:
        instructions.append("para shell, use exatamente 'run_shell' com argumento 'command'; ")
    elif has_exec_command:
        instructions.append(
            "para shell interativo, use exatamente 'exec_command' com argumento 'cmd' e write_stdin para polling; "
        )

    return "".join(instructions)


def _prune_tool_loop_messages(messages: list[dict]) -> list[dict]:
    """Limita o histórico do loop de tools preservando system/user e a cauda recente."""
    if len(messages) <= _MAX_TOOL_LOOP_MESSAGES:
        return messages
    head = messages[:2]
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
            tool_use_reliability: str = "medium",
            extra_body: Optional[dict] = None,
    ) -> None:
        """Inicializa uma instância de OpenAICompatDriver.
        extra_body: dicionário opcional mesclado no corpo da requisição (ex: {"thinking": {"type": "enabled"}})."""
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
        self.tool_use_reliability = str(tool_use_reliability or "medium").lower()
        self.extra_body = dict(extra_body) if extra_body else None

    def run(
            self,
            prompt: str,
            tool_executor=None,
            on_tool_call=None,
            on_tool_result=None,
            on_tool_abort=None,
            on_text_chunk=None,
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

        messages: list[dict] = []
        if tools:
            tool_names = [t["function"]["name"] for t in tools]
            workspace_root = getattr(getattr(tool_executor, "config", None), "workspace_root", None)
            messages.append({
                "role": "system",
                "content": _build_tool_system_prompt(tool_names, workspace_root),
            })
        messages.append({"role": "user", "content": prompt})

        max_tool_hops = get_max_tool_hops(self.tool_use_reliability)
        last_invalid_tool_name: str | None = None

        try:
            for hop in range(max_tool_hops + 1):
                if cancel_event is not None and cancel_event.is_set():
                    return None
                try:
                    response_text, tool_calls = self._chat(
                        messages,
                        tools,
                        cancel_event=cancel_event,
                        on_text_chunk=on_text_chunk if not tools else None,
                    )
                except Exception as exc:
                    _logger.error("OpenAICompatDriver: API error on hop %d: %s", hop, exc)
                    return None

                if not tool_calls:
                    return _sanitize_assistant_text(response_text) if response_text else None

                if hop == max_tool_hops:
                    _logger.warning("OpenAICompatDriver: max tool hops (%d) reached", max_tool_hops)
                    if on_tool_abort is not None:
                        on_tool_abort("max_tool_hops")
                    return _sanitize_assistant_text(
                        response_text) if response_text else "Limite de chamadas de ferramenta atingido."

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
                    if on_tool_call is not None:
                        on_tool_call(tc["name"], tc["arguments"])
                    result = self._execute_tool(tc, tool_executor)
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
                        if self.tool_use_reliability == "low" and last_invalid_tool_name == tc["name"]:
                            _logger.warning(
                                "OpenAICompatDriver: repeated invalid tool for low-reliability model: %s",
                                tc["name"],
                            )
                            if on_tool_abort is not None:
                                on_tool_abort("invalid_tool_loop")
                            return "Falha: loop de ferramenta inválida detectado."
                        last_invalid_tool_name = tc["name"]
                    else:
                        last_invalid_tool_name = None
                messages = _prune_tool_loop_messages(messages)

            return None
        finally:
            # Reseta approve-all (não-permanente) ao fim do ciclo de tool hops.
            if tool_executor is not None:
                approval_handler = getattr(tool_executor, "approval_handler", None)
                if approval_handler is not None and hasattr(approval_handler, "reset_approve_all_after_cycle"):
                    approval_handler.reset_approve_all_after_cycle()

    def _is_invalid_tool_result(self, result: ToolResult) -> bool:
        """Indica se o resultado representa uso de ferramenta fora do contrato conhecido."""
        return (not result.ok) and bool(result.error) and "Sem política para a ferramenta" in result.error

    def _chat(self, messages: list[dict], tools: list[dict], cancel_event=None, on_text_chunk=None) -> tuple[str, list[dict]]:
        """Despacha para o modo correto conforme presença de ferramentas.
        extra_body é repassado para permitir que o plugin controle parâmetros como 'thinking'."""
        # Cancelamento cooperativo: se já foi solicitado, não iniciar nova interação
        if cancel_event is not None and cancel_event.is_set():
            return "", []
        if tools:
            return self._chat_with_tools(messages, tools)
        return self._chat_streaming(messages, cancel_event=cancel_event, on_text_chunk=on_text_chunk)

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
            **( {"extra_body": self.extra_body} if self.extra_body else {} ),
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
                clean_text = _sanitize_assistant_text(_TEXT_FUNC_CALL_RE.sub("", text).strip())
                return clean_text, parsed

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
        for chunk in stream:
            if cancel_event is not None and cancel_event.is_set():
                break
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            diff = normalize_stream_diff(getattr(delta, "diff", None))

            if diff:
                text = apply_stream_diff(text, diff)
                if on_text_chunk is not None:
                    on_text_chunk({"text": content or "", "diff": diff})
                continue

            if content:
                text += content
                if on_text_chunk is not None:
                    on_text_chunk(content)
        return text.strip(), []

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
