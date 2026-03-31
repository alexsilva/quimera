from __future__ import annotations

import json
import subprocess
import time

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult


class ShellTool:
    def __init__(self, config: ToolRuntimeConfig) -> None:
        self.config = config

    def run_shell(self, call: ToolCall) -> ToolResult:
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
