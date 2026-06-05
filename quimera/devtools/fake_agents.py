"""Agentes fake para validar fluxos sem depender de provedores externos.

Este módulo expõe dois mecanismos de teste:

* ``cli``: agente CLI local que lê o prompt por stdin/argumento e devolve uma
  resposta determinística.
* ``openai-server``: servidor HTTP OpenAI-compatible mínimo que suporta
  ``/v1/models`` e ``/v1/chat/completions`` com tool calls nativas.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from quimera.prompt_templates import PromptParser

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MODEL = "quimera-fake-tools"


def _read_prompt(args: argparse.Namespace) -> str:
    if getattr(args, "prompt", None):
        return " ".join(args.prompt).strip()
    return sys.stdin.read().strip()


def run_cli_agent(args: argparse.Namespace) -> int:
    """Executa um agente CLI fake que simula papéis diferentes."""
    prompt = _read_prompt(args)
    role = args.role.strip().lower()
    print(f"[quimera-fake-cli] role={role} model={args.model}")
    print(f"Prompt recebido ({len(prompt)} chars).")

    lowered = prompt.lower()
    if role == "reviewer":
        print("Revisão fake: verifiquei o pedido e não encontrei bloqueadores determinísticos.")
    elif role == "architect":
        print("Arquitetura fake: sugiro separar entrada, roteamento, execução e evidência em camadas testáveis.")
    elif role == "tester":
        print("Teste fake: cenário executado, resultado esperado observado e evidência textual emitida.")
    elif "task" in lowered or "tarefa" in lowered:
        print("Executor fake: a tarefa foi classificada como code_edit e marcada como simulada.")
    else:
        print("Agente fake: resposta local determinística gerada com sucesso.")
    return 0




class MCPJsonRpcClient:
    """Cliente MCP mínimo sobre socket Unix para o agente CLI fake."""

    def __init__(self, socket_path: str, token: str | None = None, timeout: float = 10.0) -> None:
        if not socket_path.strip():
            raise ValueError("socket MCP não informado")
        self.socket_path = socket_path
        self.token = (token or "").strip() or None
        self.timeout = timeout
        self._next_id = 1
        self._sock: socket.socket | None = None
        self._inp = None
        self._out = None

    def __enter__(self) -> "MCPJsonRpcClient":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self._sock = sock
        self._inp = sock.makefile("r", encoding="utf-8", errors="replace")
        self._out = sock.makefile("w", encoding="utf-8")
        if self.token:
            self._out.write(json.dumps({"quimera_auth_token": self.token}) + "\n")
            self._out.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._inp is not None:
            self._inp.close()
        if self._out is not None:
            self._out.close()
        if self._sock is not None:
            self._sock.close()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._out is None:
            raise RuntimeError("cliente MCP não conectado")
        self._out.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}, ensure_ascii=False) + "\n")
        self._out.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._inp is None or self._out is None:
            raise RuntimeError("cliente MCP não conectado")
        request_id = self._next_id
        self._next_id += 1
        self._out.write(json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }, ensure_ascii=False) + "\n")
        self._out.flush()
        while True:
            raw = self._inp.readline()
            if raw == "":
                raise RuntimeError(f"conexão MCP encerrada aguardando resposta de {method}")
            msg = json.loads(raw)
            if msg.get("id") != request_id:
                continue
            if "error" in msg:
                error = msg["error"]
                raise RuntimeError(f"MCP {method} falhou: {error.get('message') or error}")
            result = msg.get("result") or {}
            return result if isinstance(result, dict) else {"value": result}

    def initialize(self) -> dict[str, Any]:
        result = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "quimera-fake-openai-mcp-cli", "version": "0.1.0"},
        })
        self.notify("notifications/initialized")
        return result

    def tools_list(self) -> list[dict[str, Any]]:
        result = self.request("tools/list")
        tools = result.get("tools") or []
        return tools if isinstance(tools, list) else []

    def tools_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})


def _mcp_tool_to_openai_schema(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
        },
    }


def _mcp_content_to_text(content: Any) -> str:
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content or "")


def _post_openai_chat(base_url: str, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer quimera-fake"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _first_choice_message(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") or []
    if not choices:
        return {"role": "assistant", "content": "[sem choices]"}
    message = choices[0].get("message") or {}
    return message if isinstance(message, dict) else {"role": "assistant", "content": str(message)}


def _tool_calls_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls") or []
    return calls if isinstance(calls, list) else []


def run_openai_mcp_cli_agent(args: argparse.Namespace) -> int:
    """Executa um CLI fake que chama OpenAI-compatible e resolve tools via MCP."""
    prompt = _read_prompt(args)
    base_url = args.base_url or os.environ.get("QUIMERA_FAKE_OPENAI_BASE_URL") or f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/v1"
    model = args.model or os.environ.get("QUIMERA_FAKE_OPENAI_MODEL") or DEFAULT_MODEL
    socket_path = args.mcp_socket or os.environ.get("QUIMERA_FAKE_MCP_SOCKET") or ""
    token = args.mcp_token or os.environ.get("QUIMERA_FAKE_MCP_TOKEN") or None
    max_tool_calls = max(args.max_tool_calls, 0)

    print(f"[quimera-fake-openai-mcp-cli] model={model} base_url={base_url}")
    if not socket_path:
        print("ERRO: QUIMERA_FAKE_MCP_SOCKET não informado; execute pelo app com MCP habilitado.")
        return 2

    with MCPJsonRpcClient(socket_path, token=token, timeout=args.timeout) as mcp:
        init = mcp.initialize()
        server_info = init.get("serverInfo") or {}
        tools = mcp.tools_list()
        openai_tools = [_mcp_tool_to_openai_schema(tool) for tool in tools if tool.get("name")]
        print(f"MCP conectado: {server_info.get('name', 'desconhecido')} | tools={len(openai_tools)}")

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        response = _post_openai_chat(base_url, {"model": model, "messages": messages, "tools": openai_tools}, timeout=args.timeout)
        message = _first_choice_message(response)
        print("OpenAI assistant:", str(message.get("content") or "").strip())

        tool_calls = _tool_calls_from_message(message)[:max_tool_calls]
        if not tool_calls:
            return 0

        messages.append(message)
        for call in tool_calls:
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            print(f"MCP tool_call: {name} {json.dumps(arguments, ensure_ascii=False)}")
            tool_result = mcp.tools_call(name, arguments)
            is_error = bool(tool_result.get("isError"))
            text = _mcp_content_to_text(tool_result.get("content"))
            print(f"MCP tool_result: {'ERRO' if is_error else 'OK'} {text[:500]}")
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": json.dumps({
                    "ok": not is_error,
                    "content": text,
                    "error": text if is_error else None,
                }, ensure_ascii=False),
            })

        final_response = _post_openai_chat(base_url, {"model": model, "messages": messages, "tools": openai_tools}, timeout=args.timeout)
        final_message = _first_choice_message(final_response)
        final_text = str(final_message.get("content") or "").strip()
        print("OpenAI final:")
        print(final_text if final_text else "[sem resposta final]")
    return 0


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _sse_response(handler: BaseHTTPRequestHandler, chunks: list[dict[str, Any]]) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()
    for chunk in chunks:
        handler.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        handler.wfile.flush()
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _last_tool_payload(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        raw = message.get("content") or "{}"
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {"ok": True, "content": str(raw)}
        return payload if isinstance(payload, dict) else {"ok": True, "content": str(payload)}
    return None


def _available_tool_names(tools: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str):
            names.add(name)
    return names


def _extract_quimera_current_turn(prompt: str) -> str:
    """Extrai o pedido atual usando o parser canônico de prompt."""
    current_turn, _ = PromptParser.extract_last_block(prompt, "current_turn")
    return current_turn or prompt


def _select_tool(prompt: str, tools: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    available = _available_tool_names(tools)
    lowered = prompt.lower()

    if "read_file" in available and "readme" in lowered:
        return "read_file", {"path": "README.md", "start_line": 1, "end_line": 40}
    if "grep_search" in available and any(token in lowered for token in ("grep", "buscar", "procure", "search")):
        return "grep_search", {"pattern": "Quimera", "path": "."}
    if "list_files" in available and any(token in lowered for token in ("listar", "liste", "arquivos", "files", "ls")):
        return "list_files", {"path": "."}
    if "run_shell" in available and any(token in lowered for token in ("pwd", "diretório", "diretorio", "shell")):
        return "run_shell", {"command": "pwd"}
    if "write_file" in available and any(token in lowered for token in ("escreva", "write", "arquivo probe")):
        return "write_file", {
            "path": "quimera_fake_probe.txt",
            "content": "Arquivo criado pelo servidor fake OpenAI-compatible.\n",
            "mode": "overwrite",
            "replace_existing": True,
        }
    return None


def _assistant_message(content: str, model: str, *, tool_call: tuple[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if tool_call is not None:
        name, arguments = tool_call
        finish_reason = "tool_calls"
        message["tool_calls"] = [
            {
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ]
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }


def _build_completion(payload: dict[str, Any]) -> dict[str, Any]:
    model = str(payload.get("model") or DEFAULT_MODEL)
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    if not isinstance(messages, list):
        messages = []
    if not isinstance(tools, list):
        tools = []

    tool_payload = _last_tool_payload(messages)
    if tool_payload is not None:
        ok = bool(tool_payload.get("ok"))
        content = str(tool_payload.get("content") or tool_payload.get("error") or "")
        status = "OK" if ok else "ERRO"
        return _assistant_message(
            f"Fake OpenAI finalizou após tool call [{status}]. Evidência retornada:\n{content[:2000]}",
            model,
        )

    prompt = _extract_quimera_current_turn(_last_user_text(messages))
    selected = _select_tool(prompt, tools)
    if selected is not None:
        name, _ = selected
        return _assistant_message(f"Vou validar usando a ferramenta {name}.", model, tool_call=selected)
    return _assistant_message(
        "Fake OpenAI respondeu sem ferramentas. Peça por README, listar arquivos, grep, pwd/shell ou write para forçar tool calling.",
        model,
    )


def _build_stream_completion(payload: dict[str, Any], completion_id: str | None = None) -> list[dict[str, Any]]:
    completion = _build_completion({**payload, "tools": []})
    model = completion["model"]
    text = completion["choices"][0]["message"].get("content") or ""
    cid = completion_id or completion["id"]
    created = int(time.time())
    chunks = []
    for part in [text[i:i + 80] for i in range(0, len(text), 80)] or [""]:
        chunks.append({
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
        })
    chunks.append({
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    return chunks


class FakeOpenAIHandler(BaseHTTPRequestHandler):
    """Handler HTTP para um backend OpenAI-compatible determinístico."""

    server_version = "QuimeraFakeOpenAI/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover - ruído de servidor local
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802 - API do BaseHTTPRequestHandler
        if self.path.rstrip("/") == "/v1/models":
            model = getattr(self.server, "model", DEFAULT_MODEL)
            _json_response(self, 200, {"object": "list", "data": [{"id": model, "object": "model"}]})
            return
        _json_response(self, 404, {"error": {"message": f"rota não encontrada: {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802 - API do BaseHTTPRequestHandler
        if self.path.rstrip("/") != "/v1/chat/completions":
            _json_response(self, 404, {"error": {"message": f"rota não encontrada: {self.path}"}})
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _json_response(self, 400, {"error": {"message": f"JSON inválido: {exc}"}})
            return
        if payload.get("stream"):
            _sse_response(self, _build_stream_completion(payload))
            return
        _json_response(self, 200, _build_completion(payload))



def run_mcp_handoff_cli_agent(args: argparse.Namespace) -> int:
    """Executa um CLI fake que delega para outro agente via call_agent no MCP."""
    prompt = _read_prompt(args)
    task = _extract_quimera_current_turn(prompt)
    target_agent = (args.target_agent or os.environ.get("QUIMERA_FAKE_HANDOFF_TARGET") or "fake-openai").strip()
    socket_path = args.mcp_socket or os.environ.get("QUIMERA_FAKE_MCP_SOCKET") or ""
    token = args.mcp_token or os.environ.get("QUIMERA_FAKE_MCP_TOKEN") or None

    print(f"[quimera-fake-mcp-handoff-cli] target={target_agent}")
    if not socket_path:
        print("ERRO: QUIMERA_FAKE_MCP_SOCKET não informado; execute pelo app com MCP habilitado.")
        return 2
    if not target_agent:
        print("ERRO: target agent não informado.")
        return 2

    arguments = {
        "agent_name": target_agent,
        "task": task,
        "context": (
            "Delegação iniciada por um agente CLI fake via MCP. "
            "O campo task contém o pedido atual extraído do prompt renderizado recebido pelo agente CLI. "
            "O agente de destino deve usar suas próprias ferramentas quando necessário."
        ),
    }
    with MCPJsonRpcClient(socket_path, token=token, timeout=args.timeout) as mcp:
        init = mcp.initialize()
        server_info = init.get("serverInfo") or {}
        tools = mcp.tools_list()
        tool_names = {str(tool.get("name") or "") for tool in tools}
        print(f"MCP conectado: {server_info.get('name', 'desconhecido')} | tools={len(tools)}")
        if "call_agent" not in tool_names:
            print("ERRO: MCP não expôs call_agent.")
            return 2
        print(f"MCP tool_call: call_agent {json.dumps(arguments, ensure_ascii=False)}")
        tool_result = mcp.tools_call("call_agent", arguments)
        is_error = bool(tool_result.get("isError"))
        text = _mcp_content_to_text(tool_result.get("content"))
        print(f"MCP tool_result: {'ERRO' if is_error else 'OK'} {text[:2000]}")
        if is_error:
            return 1
        print("Delegação finalizada via call_agent.")
        return 0

def run_openai_server(args: argparse.Namespace) -> int:
    """Inicia o servidor fake OpenAI-compatible."""
    address = (args.host, args.port)
    httpd = ThreadingHTTPServer(address, FakeOpenAIHandler)
    httpd.model = args.model  # type: ignore[attr-defined]
    httpd.quiet = args.quiet  # type: ignore[attr-defined]
    print(f"Fake OpenAI-compatible ouvindo em http://{args.host}:{args.port}/v1 (model={args.model})")
    print("Use Ctrl+C para encerrar.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando fake OpenAI-compatible.")
    finally:
        httpd.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quimera-fake", description="Agentes fake para testes interativos locais.")
    sub = parser.add_subparsers(dest="command", required=True)

    cli = sub.add_parser("cli", help="Executa um agente CLI fake local.")
    cli.add_argument("--role", default="tester", choices=["tester", "reviewer", "architect", "coder"])
    cli.add_argument("--model", default="fake-cli")
    cli.add_argument("prompt", nargs="*", help="Prompt opcional; se omitido, lê stdin.")
    cli.set_defaults(func=run_cli_agent)

    mcp_cli = sub.add_parser("openai-mcp-cli", help="CLI fake: chama OpenAI-compatible e executa tool calls via MCP.")
    mcp_cli.add_argument("--base-url", default=None)
    mcp_cli.add_argument("--model", default=None)
    mcp_cli.add_argument("--mcp-socket", default=None)
    mcp_cli.add_argument("--mcp-token", default=None)
    mcp_cli.add_argument("--timeout", type=float, default=15.0)
    mcp_cli.add_argument("--max-tool-calls", type=int, default=4)
    mcp_cli.add_argument("prompt", nargs="*", help="Prompt opcional; se omitido, lê stdin.")
    mcp_cli.set_defaults(func=run_openai_mcp_cli_agent)

    handoff_cli = sub.add_parser("mcp-handoff-cli", help="CLI fake: delega para outro agente via call_agent no MCP.")
    handoff_cli.add_argument("prompt", nargs="*", help="Prompt opcional; se omitido, lê stdin.")
    handoff_cli.add_argument("--target-agent", default="fake-openai", help="Agente alvo da tool call_agent.")
    handoff_cli.add_argument("--mcp-socket", default="", help="Socket MCP; fallback QUIMERA_FAKE_MCP_SOCKET.")
    handoff_cli.add_argument("--mcp-token", default="", help="Token MCP; fallback QUIMERA_FAKE_MCP_TOKEN.")
    handoff_cli.add_argument("--timeout", type=float, default=15.0)
    handoff_cli.set_defaults(func=run_mcp_handoff_cli_agent)

    server = sub.add_parser("openai-server", help="Inicia um backend OpenAI-compatible fake com tool calling.")
    server.add_argument("--host", default=DEFAULT_HOST)
    server.add_argument("--port", type=int, default=DEFAULT_PORT)
    server.add_argument("--model", default=DEFAULT_MODEL)
    server.add_argument("--quiet", action="store_true")
    server.set_defaults(func=run_openai_server)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
