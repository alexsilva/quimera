"""MCPServer: servidor MCP sobre stdio que expõe ferramentas do ToolExecutor.

Implementa o protocolo MCP (Model Context Protocol) versão 2025-11-25
via JSON-RPC 2.0 sobre stdio. Sem dependências externas.

Uso como módulo standalone:
    python -m quimera.runtime.mcp

Uso programático:
    executor = ToolExecutor(config, approval_handler)
    server = MCPServer(executor)
    server.serve()
"""
from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import IO, Any

from quimera.runtime.approval import ApprovalManager, TrustedToolExecutionContext
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.drivers.tool_schemas import resolve_tool_schemas
from quimera.workspace import Workspace

_logger = logging.getLogger(__name__)


def _openai_schema_to_mcp(schema: dict) -> dict:
    """Converte schema OpenAI (type/function) para formato MCP (tools/list).
    
    Inclui outputSchema (spec 2025-06-18) quando presente no schema original.
    """
    fn = schema.get("function", {})
    tool = {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "inputSchema": fn.get("parameters", {"type": "object", "properties": {}}),
    }
    output_schema = fn.get("output_schema")
    if output_schema is not None:
        tool["outputSchema"] = output_schema
    return tool


def _proxy_stdio_to_socket(
    path: str,
    *,
    token: str | None = None,
    stdin: IO | None = None,
    stdout: IO | None = None,
) -> None:
    """Faz bridge entre stdio e um servidor MCP em socket Unix.

    Se *token* for fornecido, envia a linha de autenticação antes de repassar
    stdin/stdout, conforme o protocolo de auth do MCPServer.
    """
    inp = stdin or sys.stdin
    out = stdout or sys.stdout

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    with sock:
        sock_in = sock.makefile("r", encoding="utf-8", errors="replace")
        sock_out = sock.makefile("w", encoding="utf-8")

        normalized_token = (token or "").strip() or None
        if normalized_token:
            auth_payload = {"quimera_auth_token": normalized_token}
            disabled_tools = (os.environ.get("QUIMERA_MCP_DISABLED_TOOLS") or "").strip()
            if disabled_tools:
                auth_payload["quimera_disabled_tools"] = disabled_tools
            auth_line = json.dumps(auth_payload) + "\n"
            sock_out.write(auth_line)
            sock_out.flush()

        def _pump_stdin_to_socket() -> None:
            try:
                for raw in inp:
                    sock_out.write(raw)
                    sock_out.flush()
            finally:
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        writer_thread = threading.Thread(target=_pump_stdin_to_socket, daemon=True)
        writer_thread.start()

        try:
            for raw in sock_in:
                out.write(raw)
                out.flush()
        finally:
            writer_thread.join(timeout=0.2)


class MCPServer:
    """Servidor MCP sobre stdio que expõe as ferramentas do ToolExecutor.

    Suporta os métodos obrigatórios do protocolo:
      - initialize
      - initialized (notificação, sem resposta)
      - tools/list
      - tools/call
      - ping
    """

    PROTOCOL_VERSION = "2025-11-25"
    SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
    SERVER_NAME = "quimera"
    SERVER_VERSION = "0.1.0"

    def __init__(
        self,
        tool_executor,
        *,
        auth_token: str | None = None,
        allowed_tools: Iterable[str] | None = None,
    ) -> None:
        """Inicializa uma instância de MCPServer.

        Args:
            tool_executor: Executor de ferramentas a expor via MCP.
            auth_token: Token de autenticação por sessão. Quando definido, toda
                conexão via socket Unix deve enviar uma linha JSON de autenticação
                antes de qualquer mensagem MCP.
            allowed_tools: Allowlist opcional de ferramentas expostas via MCP.
                Quando definido, ``tools/list`` filtra schemas e ``tools/call``
                rejeita nomes fora da lista.
        """
        self._executor = tool_executor
        self._write_lock = threading.Lock()
        normalized = (auth_token or "").strip()
        self._auth_token: str | None = normalized or None
        self._allowed_tools = self._normalize_allowed_tools(allowed_tools)
        self._cancel_events: dict[Any, threading.Event] = {}
        self._cancel_lock = threading.Lock()
        self._pending_calls: list[dict] = []
        self._pending_lock = threading.Lock()
        self._progress_seq = 0
        self._progress_seq_lock = threading.Lock()
        self._batch_outputs: set[int] = set()
        self._batch_outputs_lock = threading.Lock()
        self._thread_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="mcp-tool"
        )
        self._resource_subscriptions: set[str] = set()

    @staticmethod
    def _normalize_allowed_tools(
        allowed_tools: Iterable[str] | None,
    ) -> frozenset[str] | None:
        if allowed_tools is None:
            return None
        normalized = frozenset(
            str(name).strip() for name in allowed_tools if str(name).strip()
        )
        return normalized or frozenset()

    @property
    def allowed_tools(self) -> frozenset[str] | None:
        """Ferramentas permitidas via MCP; ``None`` significa sem filtro."""
        return self._allowed_tools

    @property
    def has_pending_calls(self) -> bool:
        """Indica se há tools/call ainda em execução neste servidor MCP."""
        with self._pending_lock:
            return bool(self._pending_calls)

    def set_allowed_tools(self, allowed_tools: Iterable[str] | None) -> None:
        """Atualiza a allowlist de ferramentas expostas por este servidor MCP."""
        self._allowed_tools = self._normalize_allowed_tools(allowed_tools)

    def is_tool_allowed(self, tool_name: str) -> bool:
        return self._allowed_tools is None or tool_name in self._allowed_tools

    def shutdown(self) -> None:
        """Encerra o pool de threads, aguardando tarefas em execução."""
        self._thread_pool.shutdown(wait=True)

        self._pending_calls.clear()
        self._cancel_events.clear()
    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _write(self, obj: dict | list, out: IO) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._write_lock:
            out.write(line)
            out.flush()

    # ------------------------------------------------------------------
    # Despacho de métodos
    # ------------------------------------------------------------------

    def _handle(self, msg: dict, out: IO, blocking: bool = False,
                used_ids: dict | None = None, state: dict | None = None) -> dict | None:
        validation_error = self._validate_jsonrpc_message(msg)
        msg_id = msg.get("id") if isinstance(msg, dict) else None
        if validation_error is not None:
            return self._err(msg_id, -32600, validation_error)

        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if msg_id is not None and used_ids is not None:
            if msg_id in used_ids:
                return self._err(msg_id, -32600, "Duplicate request ID")
            used_ids[msg_id] = None
            if len(used_ids) > 10_000:
                used_ids.popitem(last=False)

        if method == "initialize":
            return self._handle_initialize(msg_id, params, state)

        if method == "notifications/initialized":
            if state is not None:
                state["initialized"] = True
            return None

        if method == "notifications/cancelled":
            self._handle_cancelled(params)
            return None

        if method == "ping":
            return self._ok(msg_id, {})

        if state is not None and state.get("strict_lifecycle") and not state.get("initialize_seen"):
            return self._err(msg_id, -32002, "Server not initialized")

        if method == "tools/list":
            return self._handle_tools_list(msg_id, params, state=state)

        if method == "tools/call":
            return self._handle_tools_call(msg_id, params, out=out, blocking=blocking)

        if method == "resources/list":
            return self._handle_resources_list(msg_id, params)
        if method == "resources/read":
            return self._handle_resources_read(msg_id, params)
        if method == "resources/templates/list":
            return self._handle_resource_templates_list(msg_id, params)
        if method == "resources/subscribe":
            return self._handle_resources_subscribe(msg_id, params)
        if method == "resources/unsubscribe":
            return self._handle_resources_unsubscribe(msg_id, params)
        if method == "prompts/list":
            return self._handle_prompts_list(msg_id, params)
        if method == "prompts/get":
            return self._handle_prompts_get(msg_id, params)
        if method == "completion/complete":
            return self._handle_completion_complete(msg_id, params)

        if method == "logging/setLevel":
            return self._handle_logging_set_level(msg_id, params)

        if msg_id is not None:
            return self._err(msg_id, -32601, f"Method not found: {method}")
        return None


    def _validate_jsonrpc_message(self, msg: dict) -> str | None:
        if not isinstance(msg, dict):
            return "Invalid Request"
        if msg.get("jsonrpc") != "2.0":
            return "Invalid Request: jsonrpc must be '2.0'"
        if "method" not in msg or not isinstance(msg.get("method"), str):
            return "Invalid Request: method is required"
        if "id" in msg:
            msg_id = msg.get("id")
            if msg_id is None or isinstance(msg_id, bool) or not isinstance(msg_id, (str, int)):
                return "Invalid Request: id must be a string or integer"
        params = msg.get("params")
        if params is not None and not isinstance(params, dict):
            return "Invalid Request: params must be an object"
        return None

    def _handle_initialize(self, msg_id: Any, params: dict, state: dict | None) -> dict:
        requested = str(params.get("protocolVersion") or self.PROTOCOL_VERSION)
        selected = self.PROTOCOL_VERSION
        if state is not None:
            state["initialize_seen"] = True
            state["protocol_version"] = selected
            state["client_capabilities"] = params.get("capabilities") or {}
            state["client_info"] = params.get("clientInfo") or {}
        return self._ok(msg_id, {
            "protocolVersion": selected,
            "serverInfo": {
                "name": self.SERVER_NAME,
                "title": "Quimera MCP Server",
                "version": self.SERVER_VERSION,
                "description": "Quimera runtime tools, resources and prompts",
            },
            "instructions": "Use Quimera tools for workspace-safe inspection, execution and cross-agent delegation.",
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": True, "listChanged": True},
                "prompts": {"listChanged": True},
                "completions": {},
                "logging": {},
            },
        })

    def _handle_logging_set_level(self, msg_id: Any, params: dict) -> dict:
        level = str(params.get("level", "info")).upper()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in valid:
            return self._err(msg_id, -32602, f"Invalid logging level: {level.lower()}")
        logging.getLogger().setLevel(getattr(logging, level))
        _logger.info("MCP log level set to %s via logging/setLevel", level)
        return self._ok(msg_id, {})

    def _workspace_root(self) -> Path:
        root = getattr(getattr(self._executor, "config", None), "workspace_root", None)
        return Path(root or os.getcwd()).resolve()

    def _resource_entries(self) -> list[dict]:
        root = self._workspace_root()
        entries = [
            {"uri": "quimera://workspace", "name": "workspace", "title": "Quimera Workspace", "description": str(root), "mimeType": "text/plain"},
            {"uri": "quimera://prompts/chat", "name": "chat-prompt", "title": "Chat Prompt Template", "description": "Base chat prompt used by Quimera", "mimeType": "text/markdown"},
            {"uri": "quimera://prompts/task", "name": "task-prompt", "title": "Task Prompt Template", "description": "Prompt used for explicit /task execution", "mimeType": "text/markdown"},
            {"uri": "quimera://prompts/reviewer", "name": "task-reviewer-prompt", "title": "Task Reviewer Prompt Template", "description": "Prompt used for cross-agent task review", "mimeType": "text/markdown"},
        ]
        for rel in ("README.md", "AGENTS.md", "ARCHITECTURE.md"):
            path = root / rel
            if path.exists():
                entries.append({"uri": path.as_uri(), "name": rel, "title": rel, "description": f"Workspace file {rel}", "mimeType": "text/markdown"})
        return entries

    def _paginate(self, items: list, params: dict, key: str, page_size: int = 100) -> dict:
        cursor = params.get("cursor")
        page = 0
        if isinstance(cursor, str):
            try:
                decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
                page = int(decoded.split(":", 1)[1])
            except Exception:
                return {"__error__": "Invalid cursor"}
        start = page * page_size
        end = start + page_size
        result = {key: items[start:end]}
        if end < len(items):
            result["nextCursor"] = base64.urlsafe_b64encode(f"page:{page + 1}".encode()).decode()
        return result

    def _handle_resources_list(self, msg_id: Any, params: dict) -> dict:
        result = self._paginate(self._resource_entries(), params, "resources")
        if "__error__" in result:
            return self._err(msg_id, -32602, result["__error__"])
        return self._ok(msg_id, result)

    def _handle_resource_templates_list(self, msg_id: Any, params: dict) -> dict:
        templates = [{
            "uriTemplate": "file:///{path}",
            "name": "workspace-file",
            "title": "Workspace file",
            "description": "Read a file under the Quimera workspace by relative path",
            "mimeType": "text/plain",
        }]
        result = self._paginate(templates, params, "resourceTemplates")
        if "__error__" in result:
            return self._err(msg_id, -32602, result["__error__"])
        return self._ok(msg_id, result)

    def _read_prompt_resource(self, name: str) -> str | None:
        root = Path(__file__).resolve().parents[2]
        mapping = {
            "chat": root / "prompt.md",
            "task": root / "task_prompt.md",
            "reviewer": root / "task_reviewer_prompt.md",
        }
        path = mapping.get(name)
        if path and path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def _handle_resources_read(self, msg_id: Any, params: dict) -> dict:
        uri = str(params.get("uri") or "")
        if not uri:
            return self._err(msg_id, -32602, "resources/read: 'uri' is required")
        if uri == "quimera://workspace":
            text = str(self._workspace_root())
            return self._ok(msg_id, {"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]})
        if uri.startswith("quimera://prompts/"):
            text = self._read_prompt_resource(uri.rsplit("/", 1)[-1])
            if text is None:
                return self._err(msg_id, -32602, f"Unknown resource: {uri}")
            return self._ok(msg_id, {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]})
        if uri.startswith("file://"):
            try:
                path = Path(uri.removeprefix("file://")).resolve()
                path.relative_to(self._workspace_root())
                text = path.read_text(encoding="utf-8")
            except Exception as exc:
                return self._err(msg_id, -32602, f"Cannot read resource: {exc}")
            return self._ok(msg_id, {"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]})
        return self._err(msg_id, -32602, f"Unknown resource: {uri}")

    def _handle_resources_subscribe(self, msg_id: Any, params: dict) -> dict:
        uri = str(params.get("uri") or "")
        if not uri:
            return self._err(msg_id, -32602, "resources/subscribe: 'uri' is required")
        self._resource_subscriptions.add(uri)
        return self._ok(msg_id, {})

    def _handle_resources_unsubscribe(self, msg_id: Any, params: dict) -> dict:
        uri = str(params.get("uri") or "")
        if not uri:
            return self._err(msg_id, -32602, "resources/unsubscribe: 'uri' is required")
        self._resource_subscriptions.discard(uri)
        return self._ok(msg_id, {})

    def _prompt_entries(self) -> list[dict]:
        return [
            {"name": "quimera-chat", "title": "Quimera chat", "description": "Base prompt for a normal Quimera chat turn", "arguments": [{"name": "message", "description": "User message to append", "required": False}]},
            {"name": "quimera-task", "title": "Quimera task", "description": "Prompt for explicit /task execution", "arguments": [{"name": "task", "description": "Task description", "required": True}, {"name": "context", "description": "Optional context", "required": False}]},
            {"name": "quimera-review", "title": "Quimera review", "description": "Prompt for reviewing another agent result", "arguments": [{"name": "task", "description": "Original task", "required": True}, {"name": "result", "description": "Candidate result", "required": True}]},
        ]

    def _handle_prompts_list(self, msg_id: Any, params: dict) -> dict:
        result = self._paginate(self._prompt_entries(), params, "prompts")
        if "__error__" in result:
            return self._err(msg_id, -32602, result["__error__"])
        return self._ok(msg_id, result)

    def _handle_prompts_get(self, msg_id: Any, params: dict) -> dict:
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return self._err(msg_id, -32602, "prompts/get: 'arguments' must be an object")
        mapping = {"quimera-chat": "chat", "quimera-task": "task", "quimera-review": "reviewer"}
        key = mapping.get(name)
        if key is None:
            return self._err(msg_id, -32602, f"Unknown prompt: {name}")
        if name in {"quimera-task", "quimera-review"} and not args.get("task"):
            return self._err(msg_id, -32602, "Missing required argument: task")
        if name == "quimera-review" and not args.get("result"):
            return self._err(msg_id, -32602, "Missing required argument: result")
        template = self._read_prompt_resource(key) or ""
        suffix = "\n\n" + "\n".join(f"{k}: {v}" for k, v in args.items() if v) if args else ""
        return self._ok(msg_id, {
            "description": next(p["description"] for p in self._prompt_entries() if p["name"] == name),
            "messages": [{"role": "user", "content": {"type": "text", "text": template + suffix}}],
        })

    def _handle_completion_complete(self, msg_id: Any, params: dict) -> dict:
        ref = params.get("ref") or {}
        argument = params.get("argument") or {}
        if not isinstance(ref, dict) or not isinstance(argument, dict):
            return self._err(msg_id, -32602, "completion/complete: invalid params")
        value = str(argument.get("value") or "").lower()
        candidates: list[str]
        if ref.get("type") == "ref/prompt":
            candidates = [p["name"] for p in self._prompt_entries()] + ["message", "task", "context", "result"]
        elif ref.get("type") == "ref/resource":
            candidates = [r["uri"] for r in self._resource_entries()] + ["README.md", "AGENTS.md", "ARCHITECTURE.md"]
        else:
            return self._err(msg_id, -32602, "completion/complete: unsupported ref type")
        values = [c for c in candidates if value in c.lower()][:100]
        return self._ok(msg_id, {"completion": {"values": values, "total": len(values), "hasMore": False}})

    def _handle_cancelled(self, params: dict) -> None:
        cancel_id = params.get("requestId")
        if cancel_id is None:
            return
        with self._cancel_lock:
            event = self._cancel_events.pop(cancel_id, None)
        if event is not None:
            event.set()
            _logger.debug("MCP cancel event set for id=%s", cancel_id)

    @staticmethod
    def _disabled_tools_for_state(state: dict | None) -> frozenset[str]:
        """Retorna a denylist de tools desabilitadas para a conexão MCP."""
        if not state:
            return frozenset()
        value = state.get("quimera_disabled_tools")
        if value is None:
            return frozenset()
        if isinstance(value, str):
            names = value.split(",")
        elif isinstance(value, Iterable):
            names = value
        else:
            names = [value]
        return frozenset(str(name).strip() for name in names if str(name).strip())

    def _filter_schemas_for_state(self, schemas: list[dict], state: dict | None) -> list[dict]:
        disabled_tools = self._disabled_tools_for_state(state)
        if not disabled_tools:
            return schemas
        return [
            schema
            for schema in schemas
            if schema.get("function", {}).get("name") not in disabled_tools
        ]

    def _handle_tools_list(self, msg_id: Any, params: dict, state: dict | None = None) -> dict:
        schemas = resolve_tool_schemas(self._executor)
        if self._allowed_tools is not None:
            schemas = [
                schema
                for schema in schemas
                if schema.get("function", {}).get("name") in self._allowed_tools
            ]
        schemas = self._filter_schemas_for_state(schemas, state)
        tools = [_openai_schema_to_mcp(s) for s in schemas]
        cursor = params.get("cursor")
        page = 0
        if isinstance(cursor, str):
            try:
                decoded = base64.urlsafe_b64decode(cursor).decode()
                page = int(decoded.split(":")[1])
            except (ValueError, IndexError, Exception):
                return self._err(msg_id, -32602, "Invalid cursor")
        start = page * 100
        end = start + 100
        result: dict[str, Any] = {"tools": tools[start:end]}
        if end < len(tools):
            next_bytes = f"page:{page + 1}".encode()
            result["nextCursor"] = base64.urlsafe_b64encode(next_bytes).decode()
        return self._ok(msg_id, result)

    def _handle_tools_call(self, msg_id: Any, params: dict, out: IO, blocking: bool = False) -> dict | None:
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}
        meta = params.get("_meta") or {}
        progress_token = meta.get("progressToken") if isinstance(meta, dict) else None
        state = getattr(out, "_mcp_state", {}) or {}
        real_transport = str(state.get("transport") or "internal_mcp")
        session_id = state.get("session_id")
        trusted_context = TrustedToolExecutionContext(
            agent_name=state.get("agent_name"),
            parent_agent=state.get("parent_agent"),
            run_id=state.get("trusted_run_id") or f"{real_transport}:{uuid.uuid4()}",
            parent_run_id=state.get("parent_run_id"),
            job_id=state.get("job_id"),
            task_id=state.get("task_id"),
            transport=real_transport,
            session_id=session_id,
            server_origin="mcp_http" if real_transport == "http_mcp" else "mcp_stdio",
            http_profile=state.get("http_profile"),
            approval_scope_id=state.get("approval_scope_id"),
            delegation_budget=state.get("delegation_budget"),
            http_delegate_auto_approve=bool(state.get("http_delegate_auto_approve", False)),
        )
        call_metadata = {"trusted_context": trusted_context, "_mcp_state": state}

        if not tool_name:
            return self._err(msg_id, -32602, "tools/call: 'name' é obrigatório")
        if not isinstance(arguments, dict):
            return self._err(msg_id, -32602, "tools/call: 'arguments' must be an object")
        try:
            available_tools = set(self._executor.registry.names())
        except Exception:
            available_tools = set()
        if available_tools and tool_name not in available_tools:
            return self._err(msg_id, -32602, f"Unknown tool: {tool_name}")
        if not self.is_tool_allowed(tool_name):
            return self._err(msg_id, -32602, f"Tool not allowed: {tool_name}")
        if tool_name in self._disabled_tools_for_state(state):
            return self._err(msg_id, -32602, f"Tool disabled in this MCP context: {tool_name}")

        arg_keys = sorted(str(key) for key in arguments.keys()) if isinstance(arguments, dict) else []
        started_at = time.perf_counter()
        _logger.debug("MCP tools/call start tool=%s arg_keys=%s", tool_name, arg_keys)

        def _progress_callback(msg: str) -> None:
            _logger.debug("MCP progress [%s]: %s", tool_name, msg)
            if progress_token:
                with self._progress_seq_lock:
                    self._progress_seq += 1
                    seq = self._progress_seq
                self._write({
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": progress_token,
                        "progress": seq,
                        "message": msg,
                    }
                }, out)

        cancel_event = threading.Event()
        with self._cancel_lock:
            self._cancel_events[msg_id] = cancel_event

        future = self._thread_pool.submit(
            self._executor.execute,
            ToolCall(name=tool_name, arguments=arguments, metadata=call_metadata),
            _progress_callback,
        )

        call_info = {
            "msg_id": msg_id,
            "future": future,
            "out": out,
            "started_at": started_at,
            "tool_name": tool_name,
            "cancel_event": cancel_event,
        }
        with self._pending_lock:
            self._pending_calls.append(call_info)

        if blocking:
            return self._resolve_tool_response(call_info)

        return None

    def _resolve_tool_response(self, call: dict, wait_timeout: float = 600) -> dict | None:
        """Resolve a resposta de uma tool call (bloqueante). Retorna o dict de resposta ou None se não concluída."""
        future = call["future"]
        msg_id = call["msg_id"]
        tool_name = call["tool_name"]
        started_at = call["started_at"]
        cancel_event = call["cancel_event"]

        if not future.done():
            try:
                future.result(timeout=wait_timeout)
            except concurrent.futures.TimeoutError:
                with self._cancel_lock:
                    self._cancel_events.pop(msg_id, None)
                with self._pending_lock:
                    owned = [c for c in self._pending_calls if c["msg_id"] == msg_id]
                    for c in owned:
                        try:
                            self._pending_calls.remove(c)
                        except ValueError:
                            pass
                return self._err(msg_id, -32603, "Tool execution timed out")

        if cancel_event.is_set():
            _logger.debug("MCP tools/call cancelled tool=%s — no response sent", tool_name)
            with self._cancel_lock:
                self._cancel_events.pop(msg_id, None)
            return None

        elapsed = time.perf_counter() - started_at
        if elapsed >= wait_timeout:
            _logger.debug("MCP tools/call timeout tool=%s", tool_name)
            with self._cancel_lock:
                self._cancel_events.pop(msg_id, None)
            cancel_event.set()
            return self._err(msg_id, -32603, "Tool execution timed out")

        try:
            result = future.result(timeout=0)
        except Exception as exc:
            with self._cancel_lock:
                self._cancel_events.pop(msg_id, None)
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            _logger.debug("MCP tools/call error tool=%s duration_ms=%d", tool_name, duration_ms, exc_info=True)
            return self._err(msg_id, -32603, f"Internal error: {exc}")

        with self._cancel_lock:
            self._cancel_events.pop(msg_id, None)

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        _log = _logger.info if (not result.ok or duration_ms > 500) else _logger.debug
        _log(
            "MCP tools/call done tool=%s ok=%s duration_ms=%d",
            tool_name,
            result.ok,
            duration_ms,
        )

        if result.ok:
            text = result.content or ""
        else:
            error_str = str(result.error) if result.error else "Tool execution failed"
            text = error_str

        structured = {
            "ok": result.ok,
            "content": result.content,
        }
        if result.error is not None:
            structured["error"] = result.error
        data = getattr(result, "data", None) or {}
        if data:
            structured["data"] = data
        if getattr(result, "truncated", None) is not None:
            structured["truncated"] = result.truncated
        if getattr(result, "exit_code", None) is not None:
            structured["exit_code"] = result.exit_code

        return self._ok(msg_id, {
            "content": self._build_content(text),
            "isError": not result.ok,
            "structuredContent": structured,
        })

    def _flush_pending(self, out: IO) -> None:
        with self._batch_outputs_lock:
            if id(out) in self._batch_outputs:
                return
        with self._pending_lock:
            owned = [c for c in self._pending_calls if c["out"] is out]

        if not owned:
            return

        to_remove = []
        for call in owned:
            if not call["future"].done():
                continue
            # Marca atomicamente como "em resolução" para evitar double-write
            # quando background flush e _drain_all_pending rodam concorrentemente.
            with self._pending_lock:
                if call.get("_resolving"):
                    continue
                call["_resolving"] = True
            response = self._resolve_tool_response(call)
            if response is not None:
                self._write(response, call["out"])
            to_remove.append(call)

        if to_remove:
            with self._pending_lock:
                for call in to_remove:
                    try:
                        self._pending_calls.remove(call)
                    except ValueError:
                        pass

    def _flush_all_pending(self) -> None:
        """Faz flush de todos os pending calls para todos os `out` registrados."""
        with self._pending_lock:
            outs = list({id(c["out"]): c["out"] for c in self._pending_calls}.values())
        for out in outs:
            try:
                self._flush_pending(out)
            except Exception:
                _logger.debug("MCP flush_all error", exc_info=True)

    def _start_background_flush(self) -> None:
        """Inicia thread de flush periódico em background (idempotente).

        Necessário quando o MCPServer é usado sem `serve()` (ex: HTTP+SSE),
        para entregar respostas de tools/call assíncronas via SSE.
        """
        if getattr(self, "_bg_flush_active", False):
            return
        self._bg_flush_active = True

        def _loop() -> None:
            while self._bg_flush_active:
                time.sleep(0.05)
                try:
                    self._flush_all_pending()
                except Exception:
                    _logger.debug("MCP bg flush error", exc_info=True)

        t = threading.Thread(target=_loop, daemon=True, name="mcp-bg-flush")
        t.start()

    def _stop_background_flush(self) -> None:
        """Para a thread de flush periódico em background."""
        self._bg_flush_active = False

    def _drain_all_pending(self, out: IO) -> None:
        deadline = time.perf_counter() + 610
        while time.perf_counter() < deadline:
            self._flush_pending(out)
            with self._pending_lock:
                remaining = any(c["out"] is out for c in self._pending_calls)
            if not remaining:
                break
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # Helpers de resposta JSON-RPC 2.0
    # ------------------------------------------------------------------

    @staticmethod
    def _build_content(text: str, content_type: str = "text") -> list[dict]:
        if content_type == "image":
            return [{"type": "image", "data": text, "mimeType": "image/png"}]
        if content_type == "resource":
            return [{"type": "resource", "resource": {"text": text, "mimeType": "text/plain", "uri": "resource://quimera"}}]
        if content_type == "audio":
            return [{"type": "audio", "data": text, "mimeType": "audio/wav"}]
        return [{"type": "text", "text": text}]

    def _ok(self, msg_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _err(self, msg_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    # ------------------------------------------------------------------
    # Autenticação de socket
    # ------------------------------------------------------------------

    _AUTH_READLINE_TIMEOUT: float = 5.0

    def _authenticate_socket_connection(self, inp: IO) -> dict | None:
        """Valida a linha de autenticação na abertura de uma conexão socket.

        Retorna um dict de prelude imediatamente se auth_token não estiver
        configurado.
        Caso contrário, lê a primeira linha (com timeout) e valida o token
        sem logar seu valor.
        """
        if not self._auth_token:
            return {}
        raw_sock = getattr(inp, "buffer", None)
        raw_sock = getattr(raw_sock, "raw", None)
        underlying = getattr(raw_sock, "_sock", None) if raw_sock else None
        if underlying is None:
            underlying = getattr(inp, "_sock", None)
        previous_timeout = None
        if underlying is not None:
            try:
                previous_timeout = underlying.gettimeout()
            except Exception:
                previous_timeout = None
            try:
                underlying.settimeout(self._AUTH_READLINE_TIMEOUT)
            except Exception:
                pass
        try:
            first_line = inp.readline()
            if not first_line:
                _logger.debug("MCP auth: conexão encerrada antes da autenticação")
                return None
            payload = json.loads(first_line.strip())
            if payload.get("quimera_auth_token") == self._auth_token:
                return payload if isinstance(payload, dict) else {}
            _logger.debug("MCP auth: token inválido — conexão recusada")
            return None
        except Exception:
            _logger.debug("MCP auth: prelude inválido — conexão recusada")
            return None
        finally:
            if underlying is not None:
                try:
                    underlying.settimeout(previous_timeout)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Socket Unix
    # ------------------------------------------------------------------

    def _serve_connection(self, conn: socket.socket) -> None:
        """Serve uma conexão Unix socket até EOF."""
        try:
            with conn:
                inp = conn.makefile("r", encoding="utf-8", errors="replace")
                out = conn.makefile("w", encoding="utf-8")
                prelude = self._authenticate_socket_connection(inp)
                if prelude is None:
                    return
                disabled_tools = prelude.get("quimera_disabled_tools")
                if disabled_tools:
                    setattr(out, "_mcp_state", {"quimera_disabled_tools": disabled_tools})
                self.serve(stdin=inp, stdout=out)
        except Exception:
            _logger.debug("Conexão MCP encerrada com erro", exc_info=True)

    def serve_socket(self, path: str) -> None:
        """Escuta num socket Unix e serve cada cliente numa thread daemon.

        Bloqueia até o socket ser fechado ou o processo encerrado.
        Remove o socket anterior no path, se existir.
        """
        tmp_path = os.path.join(
            os.path.dirname(path) or ".",
            f".mcp-{os.getpid():x}-{threading.get_ident() & 0xffff:x}.sock",
        )
        for stale_path in (path, tmp_path):
            try:
                os.unlink(stale_path)
            except FileNotFoundError:
                pass

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(tmp_path)
            try:
                os.chmod(tmp_path, 0o600)
            except OSError as exc:
                _logger.debug("MCP socket: não foi possível definir permissão 0600 em %s: %s", tmp_path, exc)
            srv.listen(5)
            os.replace(tmp_path, path)
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break
                t = threading.Thread(
                    target=self._serve_connection, args=(conn,), daemon=True
                )
                t.start()
        finally:
            srv.close()
            for stale_path in (path, tmp_path):
                try:
                    os.unlink(stale_path)
                except OSError:
                    pass

    def start_background(self, path: str) -> None:
        """Inicia serve_socket em thread daemon e retorna imediatamente."""
        t = threading.Thread(target=self.serve_socket, args=(path,), daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Loop principal
    # ------------------------------------------------------------------

    def _process_message(self, msg: Any, out: IO,
                         used_ids: dict | None = None, transport: str | None = None) -> None:
        """Processa uma mensagem ou lote JSON-RPC e escreve respostas."""
        state = getattr(out, "_mcp_state", None)
        if state is None:
            state = {"initialize_seen": False, "initialized": False, "strict_lifecycle": False}
            try:
                setattr(out, "_mcp_state", state)
            except Exception:
                pass
        if transport:
            state["transport"] = transport
        else:
            state.setdefault("transport", "internal_mcp")
        if not state.get("trusted_run_id"):
            namespace = "http" if state.get("transport") == "http_mcp" else "stdio"
            state["trusted_run_id"] = f"{namespace}:{uuid.uuid4()}"
        if isinstance(msg, list):
            if not msg:
                self._write(self._err(None, -32600, "Invalid Request: empty batch"), out)
                return
            return self._process_batch(msg, out, used_ids=used_ids, state=state)

        if not isinstance(msg, dict):
            self._write(self._err(None, -32600, "Invalid Request"), out)
            return

        try:
            response = self._handle(msg, out=out, used_ids=used_ids, state=state)
        except Exception as exc:
            _logger.debug("Erro inesperado no handler MCP", exc_info=True)
            msg_id = msg.get("id")
            response = self._err(msg_id, -32603, f"Internal error: {exc}") if msg_id is not None else None

        if response is not None:
            self._write(response, out)

    def _process_batch(self, msg: list, out: IO,
                       used_ids: dict | None = None, state: dict | None = None) -> None:
        """Processa lote JSON-RPC: submete tools/call concorrentemente, resolve em bloco."""
        with self._batch_outputs_lock:
            self._batch_outputs.add(id(out))
        try:
            self._process_batch_impl(msg, out, used_ids=used_ids, state=state)
        finally:
            with self._batch_outputs_lock:
                self._batch_outputs.discard(id(out))

    def _process_batch_impl(self, msg: list, out: IO,
                            used_ids: dict | None = None, state: dict | None = None) -> None:
        responses: list[dict | None] = [None] * len(msg)
        pending_ids: dict[Any, int] = {}  # msg_id -> list index

        for idx, item in enumerate(msg):
            if not isinstance(item, dict):
                responses[idx] = self._err(None, -32600, "Invalid Request")
                continue
            try:
                item_id = item.get("id")
                if item.get("method") == "tools/call":
                    self._handle(item, out=out, blocking=False, used_ids=used_ids, state=state)
                    if item_id is not None:
                        pending_ids[item_id] = idx
                else:
                    resp = self._handle(item, out=out, used_ids=used_ids, state=state)
                    responses[idx] = resp
            except Exception as exc:
                _logger.debug("Erro inesperado no handler MCP batch", exc_info=True)
                item_id = item.get("id")
                responses[idx] = self._err(item_id, -32603, f"Internal error: {exc}") if item_id is not None else None

        if pending_ids:
            self._resolve_batch_calls(out, responses, pending_ids)

        non_null = [r for r in responses if r is not None]
        if non_null:
            self._write(non_null, out)

    def _resolve_batch_calls(self, out: IO, responses: list, pending_ids: dict) -> None:
        """Aguarda e coleta respostas de tools/call submetidas em lote."""
        with self._pending_lock:
            owned = [c for c in self._pending_calls if c["out"] is out]

        futures = [c["future"] for c in owned]
        if futures:
            concurrent.futures.wait(futures, timeout=610)

        for call in owned:
            resp = self._resolve_tool_response(call)
            idx = pending_ids.get(call["msg_id"])
            if idx is not None and resp is not None:
                responses[idx] = resp
            with self._pending_lock:
                try:
                    self._pending_calls.remove(call)
                except ValueError:
                    pass

    def serve(self, stdin: IO | None = None, stdout: IO | None = None) -> None:
        """Processa mensagens MCP até EOF no stdin.

        Suporta mensagens individuais (dict) e lotes (list) JSON-RPC 2.0.
        Tools/call é executado de forma assíncrona: o loop continua
        processando novas mensagens enquanto a tool roda em background.

        Args:
            stdin: stream de entrada (padrão: sys.stdin).
            stdout: stream de saída (padrão: sys.stdout).
        """
        inp = stdin or sys.stdin
        out = stdout or sys.stdout

        flusher_active = [True]

        def _periodic_flush() -> None:
            while flusher_active[0]:
                time.sleep(0.05)
                try:
                    self._flush_pending(out)
                except Exception:
                    _logger.debug("MCP flush error", exc_info=True)

        flush_thread = threading.Thread(target=_periodic_flush, daemon=True)
        flush_thread.start()

        used_ids: dict[Any, None] = collections.OrderedDict()

        for raw_line in inp:
            line = raw_line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                _logger.debug("JSON inválido ignorado: %s — %s", line[:80], exc)
                self._write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}, out)
                continue

            self._process_message(msg, out, used_ids=used_ids)

        flusher_active[0] = False
        self._drain_all_pending(out)


# ---------------------------------------------------------------------------
# Ponto de entrada standalone
# ---------------------------------------------------------------------------

def _build_standalone_executor():
    """Constrói um ToolExecutor mínimo para uso standalone."""
    workspace_root = Path(os.environ.get("QUIMERA_WORKSPACE", os.getcwd()))
    workspace = Workspace(workspace_root)
    config = ToolRuntimeConfig(
        workspace_root=workspace.cwd,
        db_path=workspace.tasks_db,
        memory_file=workspace.memory_file,
    )
    approval = ApprovalManager(config)
    return ToolExecutor(config, approval)


def main() -> None:
    """Inicia o MCPServer sobre stdio com executor autônomo ou proxy de socket."""
    parser = argparse.ArgumentParser(prog="python -m quimera.runtime.mcp")
    parser.add_argument(
        "--connect-socket",
        dest="connect_socket",
        default=None,
        help="Conecta no socket Unix informado e faz bridge stdio <-> socket.",
    )
    parser.add_argument(
        "--token",
        dest="token",
        default=None,
        help="Token de autenticação para o socket MCP (ou use QUIMERA_MCP_TOKEN).",
    )
    args = parser.parse_args()

    level_name = (
        os.environ.get("QUIMERA_MCP_LOG_LEVEL")
        or os.environ.get("QUIMERA_LOG_LEVEL")
        or "WARNING"
    ).upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(levelname)s mcp_server: %(message)s",
    )
    if args.connect_socket:
        token = args.token or os.environ.get("QUIMERA_MCP_TOKEN") or None
        _proxy_stdio_to_socket(args.connect_socket, token=token)
        return

    executor = _build_standalone_executor()
    server = MCPServer(executor)
    server.serve()


if __name__ == "__main__":
    main()
