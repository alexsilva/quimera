"""Componentes de `quimera.plugins.claude`."""
import json
import os
from pathlib import Path

from quimera.agent_events import SpyEvent
from quimera.plugins.base import AgentPlugin, register
from quimera.plugins.spy_utils import truncate_spy_text


def _claude_runtime_rw_paths() -> list[str]:
    """Retorna paths de estado do Claude que precisam permanecer graváveis."""
    home = Path.home()
    return [
        str(home / ".claude"),
        str(home / ".claude.json"),
        str(home / ".local" / "share" / "claude"),
    ]


def _truncate_text(value: str, limit: int = 160) -> str:
    return truncate_spy_text(value, limit=limit)


def _first_non_empty_string(payload: dict, keys: list[str]) -> str | None:
    """Retorna o primeiro valor string não-vazio encontrado pelas chaves."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _best_matching_project_key(projects: dict, cwd: str | None) -> str | None:
    """Retorna a chave de projeto mais específica (prefixo mais longo) para o cwd."""
    if not isinstance(projects, dict) or not cwd:
        return None

    try:
        cwd_norm = os.path.abspath(os.path.expanduser(str(cwd)))
    except Exception:
        return None

    best_key = None
    best_len = -1
    for raw_key in projects.keys():
        key = str(raw_key or "").strip()
        if not key:
            continue
        try:
            key_norm = os.path.abspath(os.path.expanduser(key))
        except Exception:
            continue
        if cwd_norm == key_norm or cwd_norm.startswith(f"{key_norm}{os.sep}"):
            if len(key_norm) > best_len:
                best_key = raw_key
                best_len = len(key_norm)
    return str(best_key) if best_key is not None else None


def _extract_model_from_claude_state(cwd: str | None = None, state_path: Path | None = None,
                                     settings_path: Path | None = None) -> str | None:
    """Lê melhor esforço de modelo do Claude a partir de arquivos locais."""
    settings_file = settings_path or (Path.home() / ".claude" / "settings.json")
    state_file = state_path or (Path.home() / ".claude.json")
    model_keys = ["model", "defaultModel", "default_model", "selectedModel", "currentModel"]

    try:
        settings_data = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        settings_data = {}
    if isinstance(settings_data, dict):
        from_settings = _first_non_empty_string(settings_data, model_keys)
        if from_settings:
            return from_settings

    try:
        state_data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(state_data, dict):
        return None

    root_model = _first_non_empty_string(state_data, model_keys)
    if root_model:
        return root_model

    projects = state_data.get("projects", {})
    if not isinstance(projects, dict):
        return None

    best_key = _best_matching_project_key(projects, cwd=cwd)
    project_candidates = []
    if best_key and isinstance(projects.get(best_key), dict):
        project_candidates.append(projects[best_key])

    # Fallback: varre outros projetos caso não haja match por cwd.
    for project_data in projects.values():
        if isinstance(project_data, dict) and project_data not in project_candidates:
            project_candidates.append(project_data)

    for project_data in project_candidates:
        explicit = _first_non_empty_string(project_data, model_keys)
        if explicit:
            return explicit
        usage = project_data.get("lastModelUsage")
        if isinstance(usage, dict):
            for model_name in usage.keys():
                normalized = str(model_name or "").strip()
                if normalized:
                    return normalized
    return None


def _format_claude_spy_event(line: str) -> list[SpyEvent]:
    """Resume eventos stream-json do Claude em mensagens úteis para o modo spy."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []

    etype = event.get("type")
    if etype == "result":
        if event.get("is_error"):
            detail = _truncate_text(str(event.get("result") or "erro"))
            return [SpyEvent(kind="context", text=f"execução falhou: {detail}", transient=True)]
        return [SpyEvent(kind="context", text="execução concluída", transient=True)]

    if etype != "assistant":
        return []

    messages: list[SpyEvent] = []
    content = event.get("message", {}).get("content", [])
    for block in content:
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                messages.append(SpyEvent(kind="response", text=text, final=True))
        elif btype == "tool_use":
            tool_name = block.get("name") or "ferramenta"
            messages.append(SpyEvent(kind="tool", text=f"usando {tool_name}", transient=True))
    return messages


class ClaudePlugin(AgentPlugin):
    def mcp_server_args(self, socket_path: str) -> list[str]:
        """Retorna flags para conectar o Claude ao MCP local do Quimera."""
        return ["--mcp-server", f"name=quimera,type=unix,path={socket_path}"]

    def resolve_runtime_model(self, *, cwd: str | None = None) -> str | None:
        cli_model = super().resolve_runtime_model(cwd=cwd)
        if cli_model:
            return cli_model
        return _extract_model_from_claude_state(cwd=cwd)


plugin = ClaudePlugin(
    name="claude",
    prefix="/claude",
    icon="🔮",
    runtime_rw_paths=_claude_runtime_rw_paths(),
    cmd=["claude", "--permission-mode=bypassPermissions", "--output-format=stream-json", "--verbose", "-p"],
    output_format="stream-json",
    style=("magenta", "Claude"),
    capabilities=["architecture", "code_review", "planning", "documentation", "code_editing"],
    preferred_task_types=["architecture", "code_review", "documentation", "code_edit", "general"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    tool_use_reliability="high",
    supports_code_editing=True,
    spy_stdout_formatter=_format_claude_spy_event,
    supports_long_context=True,
    base_tier=3,
)
register(plugin)
