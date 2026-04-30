"""Apresentação do stdout de agentes como eventos de spy."""

import time
from datetime import datetime, timezone

import quimera.plugins as plugins
from quimera.agent_events import SpyEvent
from quimera.constants import Visibility


class SpyOutputPresenter:
    """Converte stdout em eventos e aplica a política de visibilidade."""

    def __init__(self, renderer, visibility: Visibility):
        self.renderer = renderer
        self.visibility = visibility
        self.last_message: str | None = None
        self.pending_event: SpyEvent | None = None
        self.current_status_label = ""
        self.last_turn_detail: dict | None = None
        self._turn_seq = 0
        self._tool_seq = 0
        self.turn_id = ""
        self.turn_started_at = 0.0
        self.turn_tools: list[dict] = []
        self._active_tool_calls: dict[str, dict] = {}
        self._start_turn()

    def _start_turn(self) -> None:
        self._turn_seq += 1
        self._tool_seq = 0
        self.turn_id = f"turn_{self._turn_seq:04d}"
        self.turn_started_at = 0.0
        self.turn_tools = []
        self._active_tool_calls = {}

    @staticmethod
    def _iso(ts: float | None) -> str | None:
        if ts is None:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _clock(ts: float | None) -> str:
        if ts is None:
            ts = time.time()
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")

    def _tool_key(self, tool: str | None, tool_call_id: str | None) -> str:
        if tool_call_id:
            return tool_call_id
        self._tool_seq += 1
        return f"t_{self._tool_seq:04d}:{tool or 'tool'}"

    def _find_open_tool(self, tool: str | None) -> str | None:
        for key in reversed(list(self._active_tool_calls.keys())):
            record = self._active_tool_calls[key]
            if tool and record.get("tool") != tool:
                continue
            if record.get("ended_at") is None:
                return key
        return None

    def _find_record_by_id(self, tool_call_id: str | None) -> dict | None:
        if not tool_call_id:
            return None
        for record in reversed(self.turn_tools):
            if record.get("tool_call_id") == tool_call_id:
                return record
        return None

    def _normalize_tool_data(self, event: SpyEvent) -> dict | None:
        if event.data and isinstance(event.data, dict):
            return dict(event.data)

        text = (event.text or "").strip()
        if not text:
            return None

        if text.startswith("$ "):
            command = text[2:].strip()
            return {"tool": "exec_command", "operation": "start", "status": "running", "input": {"cmd": command}}
        if text.startswith("✓ "):
            payload = text[2:].strip()
            if payload.startswith("editar "):
                return {"tool": "apply_patch", "operation": "end", "status": "ok", "input": {"path": payload[7:]}}
            return {"tool": "exec_command", "operation": "end", "status": "ok", "input": {"cmd": payload}}
        if text.startswith("✗ "):
            payload = text[2:].strip()
            return {
                "tool": "exec_command",
                "operation": "end",
                "status": "error",
                "input": {"cmd": payload},
                "error": {"type": "ToolError", "message": payload},
            }
        if text.startswith("editar "):
            return {"tool": "apply_patch", "operation": "start", "status": "running", "input": {"path": text[7:]}}
        if text.startswith("usando "):
            return {"tool": text[7:].strip() or "ferramenta", "operation": "start", "status": "running"}
        return None

    def _record_tool_event(self, event: SpyEvent) -> None:
        if event.kind not in {"tool", "diff"}:
            return

        data = self._normalize_tool_data(event)
        if event.kind == "diff":
            if not data:
                return
            key = data.get("tool_call_id") or self._find_open_tool(data.get("tool"))
            if not key:
                return
            record = self._active_tool_calls.get(key)
            if not record:
                return
            output_meta = record.setdefault("output_meta", {})
            output_meta["diff_lines"] = int(output_meta.get("diff_lines", 0)) + 1
            return

        if not data:
            return

        tool = data.get("tool")
        operation = data.get("operation")
        status = data.get("status")
        tool_call_id = data.get("tool_call_id")
        now = time.time()

        if operation == "start":
            key = self._tool_key(tool, tool_call_id)
            record = {
                "tool_call_id": key,
                "tool": tool,
                "status": status or "running",
                "started_at": now,
                "ended_at": None,
                "duration_ms": None,
                "input": data.get("input"),
            }
            self.turn_tools.append(record)
            self._active_tool_calls[key] = record
            return

        if operation != "end":
            return

        key = tool_call_id or self._find_open_tool(tool)
        if not key:
            key = self._tool_key(tool, tool_call_id)
            record = {
                "tool_call_id": key,
                "tool": tool,
                "status": "unknown",
                "started_at": now,
                "ended_at": None,
                "duration_ms": None,
                "input": data.get("input"),
            }
            self.turn_tools.append(record)
            self._active_tool_calls[key] = record

        record = self._active_tool_calls.get(key)
        if not record:
            return

        record["status"] = status or record.get("status") or "unknown"
        record["ended_at"] = now
        started_at = record.get("started_at")
        if isinstance(started_at, (int, float)):
            record["duration_ms"] = int(max((now - started_at) * 1000, 0))
        if data.get("output_meta"):
            record["output_meta"] = data.get("output_meta")
        if data.get("error"):
            record["error"] = data.get("error")
        self._active_tool_calls.pop(key, None)

    def _timeline_text(self, event: SpyEvent) -> str | None:
        data = self._normalize_tool_data(event)
        if not data:
            return None
        operation = data.get("operation")
        tool = data.get("tool") or "ferramenta"
        tool_call_id = data.get("tool_call_id") or self._find_open_tool(tool) or "n/a"
        if operation == "start":
            cmd = (data.get("input") or {}).get("cmd")
            suffix = f" cmd={cmd}" if cmd else ""
            return f"[{self._clock(None)}] TOOL_START id={tool_call_id} tool={tool}{suffix}"
        if operation == "end":
            status = data.get("status") or "unknown"
            duration = ""
            record = self._find_record_by_id(data.get("tool_call_id"))
            if record and isinstance(record.get("duration_ms"), int):
                duration = f" duration_ms={record['duration_ms']}"
            elif record and isinstance(record.get("started_at"), (int, float)):
                duration = f" duration_ms={int(max((time.time() - record['started_at']) * 1000, 0))}"
            return f"[{self._clock(None)}] TOOL_END id={tool_call_id} status={status}{duration}"
        return None

    def build_turn_detail(self) -> dict:
        """Monta o detalhe estruturado (JSON-friendly) do turno atual."""
        tools = []
        for record in self.turn_tools:
            tools.append(
                {
                    "tool_call_id": record.get("tool_call_id"),
                    "tool": record.get("tool"),
                    "status": record.get("status"),
                    "started_at": self._iso(record.get("started_at")),
                    "ended_at": self._iso(record.get("ended_at")),
                    "duration_ms": record.get("duration_ms"),
                    "input": record.get("input"),
                    "output_meta": record.get("output_meta"),
                    "error": record.get("error"),
                }
            )
        return {"turn_id": self.turn_id, "tools": tools}

    @staticmethod
    def _format_duration(duration_ms: int | None) -> str:
        if not isinstance(duration_ms, int) or duration_ms < 0:
            return "n/a"
        if duration_ms < 1000:
            return f"{duration_ms}ms"
        return f"{duration_ms / 1000:.1f}s"

    @staticmethod
    def _format_input_summary(payload: dict | None) -> str:
        if not isinstance(payload, dict):
            return ""
        if payload.get("cmd"):
            return f"cmd: {payload['cmd']}"
        if payload.get("path"):
            return f"path: {payload['path']}"
        if payload:
            parts = []
            for key, value in payload.items():
                if value is None:
                    continue
                parts.append(f"{key}={value}")
                if len(parts) >= 2:
                    break
            if parts:
                return ", ".join(parts)
        return ""

    def _build_turn_summary_lines(self, detail: dict) -> list[str]:
        tools = detail.get("tools") if isinstance(detail, dict) else None
        if not isinstance(tools, list):
            return []
        if not tools:
            return []
        lines = [f"Ferramentas executadas neste turno ({detail.get('turn_id')}):"]
        for tool in tools:
            tool_name = tool.get("tool") or "ferramenta"
            call_id = tool.get("tool_call_id") or "n/a"
            status = tool.get("status") or "unknown"
            duration = self._format_duration(tool.get("duration_ms"))
            input_summary = self._format_input_summary(tool.get("input"))
            line = f"- {tool_name} ({call_id}) — {status} — {duration}"
            if input_summary:
                line += f" | {input_summary}"
            error = tool.get("error")
            if isinstance(error, dict) and error.get("message"):
                line += f" | erro: {error['message']}"
            lines.append(line)
        return lines

    def _render_turn_summary(self, agent: str | None, detail: dict) -> None:
        if self.visibility == Visibility.QUIET:
            return
        lines = self._build_turn_summary_lines(detail)
        for line in lines:
            self._show(agent, SpyEvent(kind="response", text=line, final=True))

    def finalize_turn(self, agent: str | None = None, render_summary: bool = False) -> dict:
        """Finaliza o turno atual e retorna o detalhe estruturado coletado."""
        self.flush(agent)
        detail = self.build_turn_detail()
        if render_summary:
            self._render_turn_summary(agent, detail)
        self.last_turn_detail = detail
        return detail

    def compose_status_label(self, base_label: str) -> str:
        """Combina o rótulo base com o status transitório atual, sem perder contexto."""
        base = (base_label or "").strip()
        current = (self.current_status_label or "").strip()
        if not current:
            return base
        if not base or current == base:
            return current
        return f"{base} | {current}"

    def format_stdout(self, agent: str | None, line: str) -> list[SpyEvent]:
        """Converte stdout cru em eventos estruturados via plugin ou fallback."""
        if not agent:
            return []
        plugin = plugins.get(agent)
        formatter = getattr(plugin, "spy_stdout_formatter", None) if plugin else None
        if callable(formatter):
            return formatter(line)

        text = line.strip()
        if not text:
            return []
        if len(text) > 200:
            text = text[:197] + "..."
        return [SpyEvent(kind="raw", text=text)]

    def consume_stdout(self, agent: str | None, line: str) -> bool:
        """Processa e emite uma linha de stdout."""
        events = self.format_stdout(agent, line)
        for event in events:
            self.emit(agent, event)
        return bool(events)

    def emit(self, agent: str | None, event: SpyEvent) -> None:
        """Renderiza um evento conforme a visibilidade configurada."""
        timeline = self._timeline_text(event) if self.visibility == Visibility.FULL else None
        self._record_tool_event(event)

        if self.visibility != Visibility.SUMMARY:
            if timeline and event.kind == "tool":
                self._show(agent, SpyEvent(kind="tool", text=timeline, transient=True, data=event.data))
            self._show(agent, event)
            return

        if event.kind == "clear":
            self.flush(agent)
            return

        if event.kind == "tool":
            self.flush(agent)
            payload = event.text.strip()
            if payload.startswith("✗ "):
                self._show(agent, event)
                self.current_status_label = ""
                return

            if payload.startswith("✓ "):
                self.current_status_label = ""
                return

            self.current_status_label = payload
            return

        if event.kind == "context":
            self.current_status_label = event.text
            return

        if event.kind == "diff":
            self.flush(agent)
            self._show(agent, event)
            return

        if event.kind != "response":
            self.flush(agent)
            self._show(agent, event)
            return

        if not event.text.strip():
            return

        self.flush(agent)
        self._show(agent, event)

    def flush(self, agent: str | None) -> None:
        """Emite o evento agrupado pendente, se existir."""
        if self.pending_event is None:
            return
        self._show(agent, self.pending_event)
        self.pending_event = None

    def reset(self) -> None:
        """Limpa estado interno entre execuções."""
        self.last_message = None
        self.pending_event = None
        self.current_status_label = ""
        self._start_turn()

    def _show(self, agent: str | None, event: SpyEvent) -> None:
        """Renderiza um evento já processado, evitando duplicatas consecutivas."""
        rendered = event.text
        dedupe_key = f"{event.kind}:{rendered}"
        if event.kind != "clear" and dedupe_key == self.last_message:
            return
        self.renderer.show_plain(rendered, agent=agent)
        self.last_message = dedupe_key
