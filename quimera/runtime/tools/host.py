"""Ferramentas read-only para diagnosticar recursos do host Linux.

A superfície é deliberadamente menor que um shell genérico: expõe métricas
estruturadas de processos e memória sem permitir sinais, mutações, leitura de
environment ou acesso irrestrito ao filesystem do host.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError
from .base import ToolBase, ValidatableTool

_HOST_TOOL_NAMES = [
    "host_processes",
    "host_process_inspect",
    "host_memory",
]

_SENSITIVE_KEY_RE = re.compile(
    r"^(?:--?)?(?:"
    r"password|passwd|token|access[-_]?token|refresh[-_]?token|"
    r"secret|api[-_]?key|client[-_]?secret|authorization|auth"
    r")$",
    re.IGNORECASE,
)
_URL_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^:/\s]+):(?P<secret>[^@\s]+)@",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization:\s*(?:bearer|basic)\s+)[^\s'\"]+",
)
_MAX_COMMAND_CHARS = 4096


def _int_value(value: str | None, default: int = 0) -> int:
    if not value:
        return default
    match = re.search(r"-?\d+", value)
    if match is None:
        return default
    try:
        return int(match.group(0))
    except ValueError:
        return default


def _read_key_value_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key] = value.strip()
    return values


def _redact_argument(argument: str) -> str:
    value = str(argument)
    key, separator, _secret = value.partition("=")
    if separator and _SENSITIVE_KEY_RE.match(key):
        return f"{key}=<redacted>"
    value = _URL_CREDENTIAL_RE.sub(
        lambda match: f"{match.group('scheme')}{match.group('user')}:<redacted>@",
        value,
    )
    value = _AUTH_HEADER_RE.sub(r"\1<redacted>", value)
    return value[:1024]


def _sanitize_argv(arguments: list[str]) -> list[str]:
    sanitized: list[str] = []
    redact_next = False
    for argument in arguments:
        value = str(argument)
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        if _SENSITIVE_KEY_RE.match(value):
            sanitized.append(value)
            redact_next = True
            continue
        sanitized.append(_redact_argument(value))
    return sanitized


def _format_command(arguments: list[str]) -> str:
    if not arguments:
        return ""
    command = shlex.join(arguments)
    if len(command) > _MAX_COMMAND_CHARS:
        return command[:_MAX_COMMAND_CHARS] + "…"
    return command


def _parse_pressure(path: Path) -> dict[str, dict[str, float | int]]:
    if not path.is_file():
        return {}
    result: dict[str, dict[str, float | int]] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        row: dict[str, float | int] = {}
        for token in parts[1:]:
            key, separator, raw = token.partition("=")
            if not separator:
                continue
            try:
                row[key] = int(raw) if key == "total" else float(raw)
            except ValueError:
                continue
        result[parts[0]] = row
    return result


class HostTools(ToolBase, tool_prefix="host"):
    """Observabilidade read-only do host, limitada a processos do mesmo usuário."""

    def __init__(
        self,
        config: ToolRuntimeConfig,
        *,
        proc_root: Path | str = Path("/proc"),
        owner_uid: int | None = None,
    ) -> None:
        super().__init__(config)
        self._proc_root = Path(proc_root)
        self._owner_uid = os.getuid() if owner_uid is None else int(owner_uid)

    def host_processes(self, call: ToolCall) -> ToolResult:
        """Lista processos visíveis pertencentes ao mesmo usuário do Quimera."""
        if not self._proc_root.is_dir():
            return self._unsupported(call.name)

        query = str(call.arguments.get("query") or "").strip().casefold()
        limit = int(call.arguments.get("limit", 100))
        sort_by = str(call.arguments.get("sort", "rss"))
        descending = bool(call.arguments.get("descending", sort_by != "pid"))

        processes: list[dict[str, Any]] = []
        try:
            entries = list(self._proc_root.iterdir())
        except OSError as exc:
            return ToolResult(ok=False, tool_name=call.name, error=f"Falha ao listar {self._proc_root}: {exc}")

        for entry in entries:
            if not entry.name.isdigit() or not entry.is_dir():
                continue
            process = self._read_process(entry)
            if process is None:
                continue
            haystack = f"{process['pid']} {process['name']} {process['command']}".casefold()
            if query and query not in haystack:
                continue
            processes.append(process)

        sort_keys = {
            "rss": lambda item: item["rss_kb"],
            "hwm": lambda item: item["hwm_kb"],
            "pid": lambda item: item["pid"],
            "name": lambda item: item["name"].casefold(),
        }
        processes.sort(key=sort_keys[sort_by], reverse=descending)
        total = len(processes)
        visible = processes[:limit]

        content = "\n".join(
            (
                f"pid={item['pid']} ppid={item['ppid']} rss={item['rss_kb']}kB "
                f"hwm={item['hwm_kb']}kB threads={item['threads']} "
                f"state={item['state']} name={item['name']} command={item['command']}"
            ).rstrip()
            for item in visible
        )
        payload = {
            "uid": self._owner_uid,
            "same_user_only": True,
            "query": query,
            "sort": sort_by,
            "descending": descending,
            "total": total,
            "returned": len(visible),
            "processes": visible,
        }
        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=content or "Nenhum processo correspondente.",
            truncated=total > len(visible),
            data=payload,
        )

    def host_process_inspect(self, call: ToolCall) -> ToolResult:
        """Inspeciona memória, FDs, inotify e filhos de um processo do mesmo usuário."""
        if not self._proc_root.is_dir():
            return self._unsupported(call.name)

        pid = int(call.arguments["pid"])
        process_root = self._proc_root / str(pid)
        process = self._read_process(process_root)
        if process is None:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=f"Processo {pid} não existe, encerrou ou não pertence ao usuário atual.",
            )

        process["vm_size_kb"] = self._status_number(process_root, "VmSize")
        process["vm_data_kb"] = self._status_number(process_root, "VmData")
        process["swap_kb"] = self._status_number(process_root, "VmSwap")
        process["cwd"] = self._safe_readlink(process_root / "cwd")
        process["executable"] = self._safe_readlink(process_root / "exe")
        process["children"] = self._read_children(process_root, pid)
        process["fds"] = self._inspect_fds(process_root)

        fds = process["fds"]
        content = (
            f"pid={pid} ppid={process['ppid']} state={process['state']} name={process['name']}\n"
            f"rss={process['rss_kb']}kB hwm={process['hwm_kb']}kB "
            f"vm_size={process['vm_size_kb']}kB vm_data={process['vm_data_kb']}kB "
            f"swap={process['swap_kb']}kB threads={process['threads']}\n"
            f"fds={fds['total']} sockets={fds['sockets']} pipes={fds['pipes']} "
            f"inotify_fds={fds['inotify_fds']} inotify_watches={fds['inotify_watches']}\n"
            f"children={process['children']}\n"
            f"cwd={process['cwd'] or '-'}\n"
            f"executable={process['executable'] or '-'}\n"
            f"command={process['command']}"
        )
        return ToolResult(ok=True, tool_name=call.name, content=content, data=process)

    def host_memory(self, call: ToolCall) -> ToolResult:
        """Retorna RAM, swap, load average e PSI de memória do host."""
        if not self._proc_root.is_dir():
            return self._unsupported(call.name)
        meminfo_path = self._proc_root / "meminfo"
        try:
            meminfo = _read_key_value_file(meminfo_path)
        except OSError as exc:
            return ToolResult(ok=False, tool_name=call.name, error=f"Falha ao ler {meminfo_path}: {exc}")

        total_kb = _int_value(meminfo.get("MemTotal"))
        available_kb = _int_value(meminfo.get("MemAvailable"))
        free_kb = _int_value(meminfo.get("MemFree"))
        swap_total_kb = _int_value(meminfo.get("SwapTotal"))
        swap_free_kb = _int_value(meminfo.get("SwapFree"))
        used_kb = max(0, total_kb - available_kb) if total_kb else 0
        swap_used_kb = max(0, swap_total_kb - swap_free_kb) if swap_total_kb else 0

        loadavg = ""
        try:
            loadavg = (self._proc_root / "loadavg").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass

        uptime_seconds: float | None = None
        try:
            raw_uptime = (self._proc_root / "uptime").read_text(encoding="utf-8", errors="replace").split()[0]
            uptime_seconds = float(raw_uptime)
        except (OSError, ValueError, IndexError):
            pass

        pressure = _parse_pressure(self._proc_root / "pressure" / "memory")
        payload = {
            "mem_total_kb": total_kb,
            "mem_available_kb": available_kb,
            "mem_used_kb": used_kb,
            "mem_free_kb": free_kb,
            "cached_kb": _int_value(meminfo.get("Cached")),
            "buffers_kb": _int_value(meminfo.get("Buffers")),
            "swap_total_kb": swap_total_kb,
            "swap_free_kb": swap_free_kb,
            "swap_used_kb": swap_used_kb,
            "loadavg": loadavg,
            "uptime_seconds": uptime_seconds,
            "pressure": pressure,
        }
        content = (
            f"memory used={used_kb}kB available={available_kb}kB total={total_kb}kB\n"
            f"swap used={swap_used_kb}kB free={swap_free_kb}kB total={swap_total_kb}kB\n"
            f"loadavg={loadavg or '-'}\n"
            f"memory_pressure={pressure or {}}"
        )
        return ToolResult(ok=True, tool_name=call.name, content=content, data=payload)

    def _read_process(self, process_root: Path) -> dict[str, Any] | None:
        try:
            status = _read_key_value_file(process_root / "status")
            uid = _int_value(status.get("Uid"), default=-1)
            if uid != self._owner_uid:
                return None
            pid = int(process_root.name)
            arguments = self._read_cmdline(process_root / "cmdline")
            name = status.get("Name") or self._read_comm(process_root / "comm") or ""
            return {
                "pid": pid,
                "ppid": _int_value(status.get("PPid")),
                "uid": uid,
                "name": name,
                "state": (status.get("State") or "").split()[0],
                "rss_kb": _int_value(status.get("VmRSS")),
                "hwm_kb": _int_value(status.get("VmHWM")),
                "threads": _int_value(status.get("Threads")),
                "command": _format_command(_sanitize_argv(arguments)),
            }
        except (OSError, ValueError):
            return None

    @staticmethod
    def _read_cmdline(path: Path) -> list[str]:
        try:
            payload = path.read_bytes()
        except OSError:
            return []
        return [
            chunk.decode("utf-8", errors="replace")
            for chunk in payload.split(b"\0")
            if chunk
        ]

    @staticmethod
    def _read_comm(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    @staticmethod
    def _safe_readlink(path: Path) -> str | None:
        try:
            return os.readlink(path)
        except OSError:
            return None

    @staticmethod
    def _read_children(process_root: Path, pid: int) -> list[int]:
        path = process_root / "task" / str(pid) / "children"
        try:
            return [int(value) for value in path.read_text(encoding="utf-8", errors="replace").split()]
        except (OSError, ValueError):
            return []

    def _status_number(self, process_root: Path, key: str) -> int:
        try:
            return _int_value(_read_key_value_file(process_root / "status").get(key))
        except OSError:
            return 0

    @staticmethod
    def _inspect_fds(process_root: Path) -> dict[str, int]:
        result = {
            "total": 0,
            "files": 0,
            "sockets": 0,
            "pipes": 0,
            "anon": 0,
            "inotify_fds": 0,
            "inotify_watches": 0,
            "unreadable": 0,
        }
        fd_root = process_root / "fd"
        try:
            entries = list(fd_root.iterdir())
        except OSError:
            return result
        result["total"] = len(entries)

        for entry in entries:
            try:
                target = os.readlink(entry)
            except OSError:
                result["unreadable"] += 1
                continue
            if target.startswith("socket:"):
                result["sockets"] += 1
            elif target.startswith("pipe:"):
                result["pipes"] += 1
            elif target == "anon_inode:inotify":
                result["anon"] += 1
                result["inotify_fds"] += 1
                try:
                    fdinfo = (process_root / "fdinfo" / entry.name).read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    continue
                result["inotify_watches"] += sum(
                    1 for line in fdinfo.splitlines() if line.startswith("inotify wd:")
                )
            elif target.startswith("anon_inode:"):
                result["anon"] += 1
            else:
                result["files"] += 1
        return result

    @staticmethod
    def _unsupported(tool_name: str) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error="Diagnóstico de host requer um sistema Linux com /proc disponível.",
        )


class HostToolsValidator(ValidatableTool):
    """Valida apenas argumentos; as tools são estritamente read-only."""

    _SORTS = frozenset({"rss", "hwm", "pid", "name"})

    def _validate_host_processes(self, call: ToolCall) -> None:
        query = str(call.arguments.get("query") or "")
        if len(query) > 200:
            raise ToolPolicyError("host_processes.query aceita no máximo 200 caracteres")
        try:
            limit = int(call.arguments.get("limit", 100))
        except (TypeError, ValueError) as exc:
            raise ToolPolicyError("host_processes.limit deve ser inteiro") from exc
        if not 1 <= limit <= 500:
            raise ToolPolicyError("host_processes.limit deve estar entre 1 e 500")
        sort_by = str(call.arguments.get("sort", "rss"))
        if sort_by not in self._SORTS:
            raise ToolPolicyError(f"host_processes.sort deve ser um de {sorted(self._SORTS)}")

    def _validate_host_process_inspect(self, call: ToolCall) -> None:
        if "pid" not in call.arguments:
            raise ToolPolicyError("host_process_inspect requer 'pid'")
        try:
            pid = int(call.arguments["pid"])
        except (TypeError, ValueError) as exc:
            raise ToolPolicyError("host_process_inspect.pid deve ser inteiro positivo") from exc
        if pid <= 0:
            raise ToolPolicyError("host_process_inspect.pid deve ser inteiro positivo")

    def _validate_host_memory(self, call: ToolCall) -> None:
        return


def register(registry, policy, config) -> None:
    """Registra as tools read-only de diagnóstico do host."""
    tools = HostTools(config)
    validator = HostToolsValidator(config)
    for name in _HOST_TOOL_NAMES:
        registry.register(name, getattr(tools, name))
    policy.register_tool_validator(_HOST_TOOL_NAMES, validator)
