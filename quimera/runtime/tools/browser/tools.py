"""Tool wrappers and policy validation for browser automation."""
from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

from ...config import ToolRuntimeConfig
from ...models import ToolCall, ToolResult
from ...policy import ToolPolicyError, is_path_inside
from ..base import ToolBase, ValidatableTool
from .service import BrowserService


_BROWSER_TOOL_NAMES = (
    "browser_start",
    "browser_status",
    "browser_close",
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_mouse",
    "browser_wait",
    "browser_evaluate",
    "browser_screenshot",
    "browser_console",
    "browser_network",
)


class BrowserTool(
    ToolBase,
    tool_prefix="browser",
    tool_public_methods=("shutdown",),
):
    """Persistent browser sessions for UI, canvas, console and network tests."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        super().__init__(config)
        self._service = BrowserService(config.workspace_root)

    def __del__(self) -> None:
        try:
            self.shutdown()
        except Exception:  # noqa: BLE001 - best-effort interpreter cleanup
            pass

    def shutdown(self) -> None:
        """Encerra todas as sessões e finaliza o worker Playwright."""
        service = getattr(self, "_service", None)
        if service is not None:
            service.shutdown()

    def browser_start(self, call: ToolCall) -> ToolResult:
        return self._execute(call, "start", timeout_seconds=75)

    def browser_status(self, call: ToolCall) -> ToolResult:
        return self._execute(call, "status")

    def browser_close(self, call: ToolCall) -> ToolResult:
        return self._execute(call, "close")

    def browser_navigate(self, call: ToolCall) -> ToolResult:
        timeout = self._tool_timeout(call, default_ms=30_000)
        return self._execute(call, "navigate", timeout_seconds=timeout)

    def browser_snapshot(self, call: ToolCall) -> ToolResult:
        result = self._execute(call, "snapshot")
        if not result.ok:
            return result
        data = result.data
        lines = [
            f"URL: {data.get('url', '')}",
            f"Título: {data.get('title', '')}",
            "",
            str(data.get("text", "")),
            "",
            "Elementos interativos:",
        ]
        for element in data.get("elements", []):
            label = element.get("text") or element.get("aria_label") or element.get("name") or ""
            lines.append(
                f"[{element.get('index')}] {element.get('tag')} "
                f"{element.get('selector')} {label!r} bounds={element.get('bounds')}"
            )
        result.content = "\n".join(lines).strip()
        return result

    def browser_click(self, call: ToolCall) -> ToolResult:
        return self._execute(call, "click")

    def browser_type(self, call: ToolCall) -> ToolResult:
        return self._execute(call, "type")

    def browser_press(self, call: ToolCall) -> ToolResult:
        return self._execute(call, "press")

    def browser_mouse(self, call: ToolCall) -> ToolResult:
        return self._execute(call, "mouse")

    def browser_wait(self, call: ToolCall) -> ToolResult:
        timeout = self._tool_timeout(call, default_ms=15_000)
        return self._execute(call, "wait", timeout_seconds=timeout)

    def browser_evaluate(self, call: ToolCall) -> ToolResult:
        result = self._execute(call, "evaluate")
        if result.ok:
            result.content = json.dumps(result.data.get("value"), ensure_ascii=False, indent=2)
        return result

    def browser_screenshot(self, call: ToolCall) -> ToolResult:
        arguments = dict(call.arguments)
        session_id = str(arguments.get("session_id", "browser"))
        raw_path = str(arguments.get("path") or "").strip()
        if not raw_path:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            raw_path = f"browser/{session_id}/{stamp}.png"
        artifacts_root = self.config.artifacts_root
        output_path = (artifacts_root / raw_path.lstrip("/")).resolve()
        if not is_path_inside(output_path, artifacts_root):
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=f"Screenshot fora do diretório de artefatos: {raw_path}",
            )
        if output_path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            output_path = output_path.with_suffix(".png")
        arguments["path"] = str(output_path)
        result = self._execute(
            ToolCall(call.name, arguments, call_id=call.call_id, metadata=call.metadata),
            "screenshot",
        )
        if result.ok:
            result.data["path"] = str(output_path)
            result.content = f"Screenshot salvo em {output_path} ({result.data.get('bytes', 0)} bytes)."
        return result

    def browser_console(self, call: ToolCall) -> ToolResult:
        result = self._execute(call, "console")
        if result.ok:
            result.content = self._format_events(result.data.get("events", []))
        return result

    def browser_network(self, call: ToolCall) -> ToolResult:
        result = self._execute(call, "network")
        if result.ok:
            result.content = self._format_events(result.data.get("events", []))
        return result

    def _execute(
        self,
        call: ToolCall,
        operation: str,
        *,
        timeout_seconds: float = 45,
    ) -> ToolResult:
        try:
            data = self._service.execute(
                operation,
                call.arguments,
                timeout_seconds=timeout_seconds,
            )
        except ModuleNotFoundError:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=(
                    "Playwright não está instalado. Instale o extra browser com "
                    "'pip install -e .[browser]'."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - converted to a structured tool failure
            return ToolResult(ok=False, tool_name=call.name, error=f"Falha no navegador: {exc}")

        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=self._default_content(operation, data),
            data=data,
        )

    @staticmethod
    def _tool_timeout(call: ToolCall, *, default_ms: int) -> float:
        timeout_ms = int(call.arguments.get("timeout_ms", default_ms))
        return max(5.0, timeout_ms / 1000 + 10.0)

    @staticmethod
    def _default_content(operation: str, data: dict[str, Any]) -> str:
        if operation == "start":
            return (
                f"Sessão {data.get('session_id')} iniciada em {data.get('url')} "
                f"({data.get('title', '')})."
            )
        if operation == "status":
            sessions = data.get("sessions", [])
            if not sessions:
                return "Nenhuma sessão de navegador ativa."
            return "\n".join(
                f"{item['session_id']}: {item.get('title', '')} — {item.get('url', '')}"
                for item in sessions
            )
        if operation == "close":
            return f"Sessão {data.get('session_id')} encerrada."
        return f"Operação {operation} concluída em {data.get('url', '')}."

    @staticmethod
    def _format_events(events: list[dict[str, Any]]) -> str:
        if not events:
            return "Nenhum evento registrado."
        return "\n".join(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in events)


class BrowserToolValidator(ValidatableTool):
    """Validates browser sessions, selectors, URLs and output paths."""

    _WAIT_UNTIL = {"commit", "domcontentloaded", "load", "networkidle"}
    _MOUSE_ACTIONS = {"move", "down", "up", "click", "wheel"}
    _BUTTONS = {"left", "middle", "right"}

    def _validate_browser_start(self, call: ToolCall) -> None:
        if call.arguments.get("url"):
            self._validate_url(str(call.arguments["url"]))
        self._validate_viewport(call)
        self._validate_wait_until(call)

    def _validate_browser_status(self, call: ToolCall) -> None:
        return None

    def _validate_browser_close(self, call: ToolCall) -> None:
        self._validate_session(call)

    def _validate_browser_navigate(self, call: ToolCall) -> None:
        self._validate_session(call)
        self._validate_url(str(call.arguments.get("url", "")))
        self._validate_wait_until(call)

    def _validate_browser_snapshot(self, call: ToolCall) -> None:
        self._validate_session(call)
        self._validate_optional_selector(call)

    def _validate_browser_click(self, call: ToolCall) -> None:
        self._validate_session(call)
        selector = str(call.arguments.get("selector", "")).strip()
        has_coordinates = "x" in call.arguments and "y" in call.arguments
        if not selector and not has_coordinates:
            raise ToolPolicyError("browser_click requer 'selector' ou coordenadas 'x' e 'y'")
        self._validate_button(call)

    def _validate_browser_type(self, call: ToolCall) -> None:
        self._validate_session(call)
        self._require_text(call, "selector")
        if "text" not in call.arguments:
            raise ToolPolicyError("browser_type requer 'text'")

    def _validate_browser_press(self, call: ToolCall) -> None:
        self._validate_session(call)
        self._require_text(call, "key")
        self._validate_optional_selector(call)

    def _validate_browser_mouse(self, call: ToolCall) -> None:
        self._validate_session(call)
        action = str(call.arguments.get("action", ""))
        if action not in self._MOUSE_ACTIONS:
            raise ToolPolicyError(f"browser_mouse action deve ser uma de {sorted(self._MOUSE_ACTIONS)}")
        if action in {"move", "click"} and not {"x", "y"} <= call.arguments.keys():
            raise ToolPolicyError(f"browser_mouse action={action} requer x e y")
        self._validate_button(call)

    def _validate_browser_wait(self, call: ToolCall) -> None:
        self._validate_session(call)
        self._validate_optional_selector(call)
        state = str(call.arguments.get("state", "visible"))
        if state not in {"attached", "detached", "visible", "hidden"}:
            raise ToolPolicyError("browser_wait state inválido")

    def _validate_browser_evaluate(self, call: ToolCall) -> None:
        self._validate_session(call)
        self._require_text(call, "expression")

    def _validate_browser_screenshot(self, call: ToolCall) -> None:
        self._validate_session(call)
        self._validate_optional_selector(call)
        raw_path = str(call.arguments.get("path", "")).strip()
        if raw_path:
            artifacts_root = self.config.artifacts_root
            path = (artifacts_root / raw_path.lstrip("/")).resolve()
            if not is_path_inside(path, artifacts_root):
                raise ToolPolicyError(f"Screenshot fora do diretório de artefatos: {raw_path}")

    def _validate_browser_console(self, call: ToolCall) -> None:
        self._validate_session(call)

    def _validate_browser_network(self, call: ToolCall) -> None:
        self._validate_session(call)

    def _validate_session(self, call: ToolCall) -> None:
        self._require_text(call, "session_id")

    @staticmethod
    def _require_text(call: ToolCall, name: str) -> None:
        value = call.arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ToolPolicyError(f"{call.name} requer '{name}' não vazio")

    @staticmethod
    def _validate_optional_selector(call: ToolCall) -> None:
        selector = call.arguments.get("selector")
        if selector is not None and (not isinstance(selector, str) or not selector.strip()):
            raise ToolPolicyError(f"{call.name} recebeu selector vazio")

    def _validate_url(self, raw_url: str) -> None:
        if not raw_url.strip():
            raise ToolPolicyError("URL do navegador não pode ser vazia")
        parsed = urllib.parse.urlparse(raw_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return
        if raw_url == "about:blank":
            return
        if parsed.scheme == "file":
            path = Path(urllib.parse.unquote(parsed.path)).resolve()
            if is_path_inside(path, self.config.workspace_root):
                return
            raise ToolPolicyError(f"URL file fora da workspace: {path}")
        raise ToolPolicyError("browser aceita apenas http://, https://, about:blank ou file:// dentro da workspace")

    def _validate_viewport(self, call: ToolCall) -> None:
        for name, minimum, maximum in (("width", 240, 7680), ("height", 200, 4320)):
            value = int(call.arguments.get(name, 1280 if name == "width" else 720))
            if not minimum <= value <= maximum:
                raise ToolPolicyError(f"browser_start {name} deve estar entre {minimum} e {maximum}")

    def _validate_wait_until(self, call: ToolCall) -> None:
        value = str(call.arguments.get("wait_until", "load"))
        if value not in self._WAIT_UNTIL:
            raise ToolPolicyError(f"wait_until deve ser um de {sorted(self._WAIT_UNTIL)}")

    def _validate_button(self, call: ToolCall) -> None:
        button = str(call.arguments.get("button", "left"))
        if button not in self._BUTTONS:
            raise ToolPolicyError(f"button deve ser um de {sorted(self._BUTTONS)}")


def register(registry, policy, config: ToolRuntimeConfig) -> BrowserTool:
    """Registers browser tools and their shared validator."""
    tools = BrowserTool(config)
    validator = BrowserToolValidator(config)
    for name in _BROWSER_TOOL_NAMES:
        registry.register(name, getattr(tools, name))
    policy.register_tool_validator(list(_BROWSER_TOOL_NAMES), validator)
    return tools
