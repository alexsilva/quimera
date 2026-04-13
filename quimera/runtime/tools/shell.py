"""Componentes de `quimera.runtime.tools.shell`."""
from __future__ import annotations

import json
import subprocess
import time
import warnings

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from . import files as file_tools


class ShellTool:
    """Implementa `ShellTool`."""
    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de ShellTool."""
        self.config = config

    def run_shell(self, call: ToolCall) -> ToolResult:
        """Executa shell."""
        staging = file_tools.get_staging_root()
        if staging:
            warnings.warn(
                f"run_shell called in parallel mode with staging - cwd={self.config.workspace_root}, "
                f"staging={staging}. Shell writes bypass staging isolation.",
                UserWarning,
                stacklevel=2,
            )
        
        command = str(call.arguments["command"])
        started = time.perf_counter()
        proc = subprocess.run(
            command,
            shell=True,
            cwd=self.config.workspace_root,
            capture_output=True,
            text=True,
            timeout=self.config.command_timeout_seconds,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        payload = json.dumps(
            {
                "command": command,
                "cwd": str(self.config.workspace_root),
                "stdout": stdout[: self.config.max_output_chars],
                "stderr": stderr[: self.config.max_output_chars],
            },
            ensure_ascii=False,
            indent=2,
        )
        truncated = len(stdout) > self.config.max_output_chars or len(stderr) > self.config.max_output_chars
        return ToolResult(
            ok=proc.returncode == 0,
            tool_name=call.name,
            content=payload,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            truncated=truncated,
        )
