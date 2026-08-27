"""Ferramentas read-only para diagnosticar recursos do host Linux.

A superfície é deliberadamente menor que um shell genérico: expõe métricas
estruturadas de processos e memória sem permitir sinais, mutações, leitura de
environment ou acesso irrestrito ao filesystem do host.
"""
from __future__ import annotations

import os
import re
import shlex
import time
from collections.abc import Callable
from math import ceil
from pathlib import Path
from typing import Any

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError
from .base import ToolBase, ValidatableTool
from .host_sampling import (
    cpu_percent,
    format_sample_summary,
    growth_signals,
    summarize_samples,
)

_HOST_TOOL_NAMES = [
    "host_processes",
    "host_process_inspect",
    "host_process_sample",
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
_MAX_PROCESS_SAMPLES = 121


def _clock_ticks_per_second() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, TypeError):
        return 100.0


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
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_ticks_per_second: float | None = None,
    ) -> None:
        super().__init__(config)
        self._proc_root = Path(proc_root)
        self._owner_uid = os.getuid() if owner_uid is None else int(owner_uid)
        self._monotonic = monotonic_fn
        self._sleep = sleep_fn
        self._clock_ticks_per_second = (
            float(clock_ticks_per_second)
            if clock_ticks_per_second is not None
            else _clock_ticks_per_second()
        )
        self._progress_callback: Callable[[str], None] | None = None
        self._cancel_checker: Callable[[], bool] | None = None

    def _set_progress_callback(self, callback: Callable[[str], None] | None) -> None:
        """Injeta callback de progresso para operações de amostragem."""
        self._progress_callback = callback

    def _set_cancel_checker(self, checker: Callable[[], bool] | None) -> None:
        """Injeta cancelamento cooperativo usado fora do transporte MCP."""
        self._cancel_checker = checker

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

    def host_process_sample(self, call: ToolCall) -> ToolResult:
        """Amostra um processo ao longo do tempo e calcula deltas/tendências.

        A operação permanece read-only. O resultado não interpreta crescimento
        como leak: expõe somente medidas objetivas (delta, min/max e slope),
        deixando a conclusão causal para quem estiver conduzindo o diagnóstico.
        """
        if not self._proc_root.is_dir():
            return self._unsupported(call.name)

        pid = int(call.arguments["pid"])
        duration_seconds = float(call.arguments.get("duration_seconds", 5.0))
        interval_ms = int(call.arguments.get("interval_ms", 500))
        include_fds = bool(call.arguments.get("include_fds", True))
        interval_seconds = interval_ms / 1000.0
        process_root = self._proc_root / str(pid)

        first_identity = self._read_process_identity(process_root)
        if first_identity is None:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=f"Processo {pid} não existe, encerrou ou não pertence ao usuário atual.",
            )

        started_at = self._monotonic()
        samples: list[dict[str, Any]] = []
        ended = False
        cancelled = False
        pid_reused = False
        target_sample_count = ceil(duration_seconds / interval_seconds) + 1

        while len(samples) < target_sample_count:
            if self._is_cancelled(call):
                cancelled = True
                break

            identity = self._read_process_identity(process_root)
            if identity is None:
                ended = True
                break
            if identity["start_ticks"] != first_identity["start_ticks"]:
                pid_reused = True
                break

            now = self._monotonic()
            elapsed = max(0.0, now - started_at)
            sample = self._read_process_sample(
                process_root,
                pid=pid,
                elapsed_seconds=elapsed,
                include_fds=include_fds,
            )
            if sample is None:
                ended = True
                break
            if samples:
                sample["cpu_percent"] = cpu_percent(samples[-1], sample)
            else:
                sample["cpu_percent"] = None
            samples.append(sample)
            self._report_sample_progress(
                pid,
                sample,
                current=len(samples),
                total=target_sample_count,
            )

            if len(samples) >= target_sample_count:
                break
            target_elapsed = min(duration_seconds, len(samples) * interval_seconds)
            delay = target_elapsed - max(0.0, self._monotonic() - started_at)
            if delay > 0 and self._wait_cooperatively(call, delay):
                cancelled = True
                break

        if not samples:
            state = "cancelado" if cancelled else "encerrado"
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=f"Processo {pid} foi {state} antes da primeira amostra útil.",
            )

        actual_duration = samples[-1]["elapsed_seconds"] - samples[0]["elapsed_seconds"]
        summary = summarize_samples(samples)
        signals = growth_signals(summary)
        payload = {
            "pid": pid,
            "uid": self._owner_uid,
            "same_user_only": True,
            "name": first_identity["name"],
            "command": first_identity["command"],
            "requested_duration_seconds": duration_seconds,
            "actual_duration_seconds": round(max(0.0, actual_duration), 6),
            "interval_ms": interval_ms,
            "include_fds": include_fds,
            "sample_count": len(samples),
            "target_sample_count": target_sample_count,
            "ended": ended,
            "cancelled": cancelled,
            "pid_reused": pid_reused,
            "samples": samples,
            "summary": summary,
            "growth_observed": signals,
        }
        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=format_sample_summary(payload),
            data=payload,
        )

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

    def _read_process_identity(self, process_root: Path) -> dict[str, Any] | None:
        process = self._read_process(process_root)
        stat = self._read_process_stat(process_root)
        if process is None or stat is None:
            return None
        return {
            "pid": process["pid"],
            "uid": process["uid"],
            "name": process["name"],
            "command": process["command"],
            "start_ticks": stat["start_ticks"],
        }

    def _read_process_sample(
        self,
        process_root: Path,
        *,
        pid: int,
        elapsed_seconds: float,
        include_fds: bool,
    ) -> dict[str, Any] | None:
        process = self._read_process(process_root)
        stat = self._read_process_stat(process_root)
        if process is None or stat is None:
            return None

        fds = self._inspect_fds(process_root) if include_fds else None
        cpu_time_seconds = (
            stat["utime_ticks"] + stat["stime_ticks"]
        ) / self._clock_ticks_per_second
        sample: dict[str, Any] = {
            "elapsed_seconds": round(elapsed_seconds, 6),
            "rss_kb": process["rss_kb"],
            "hwm_kb": process["hwm_kb"],
            "vm_size_kb": self._status_number(process_root, "VmSize"),
            "vm_data_kb": self._status_number(process_root, "VmData"),
            "swap_kb": self._status_number(process_root, "VmSwap"),
            "threads": process["threads"],
            "children": len(self._read_children(process_root, pid)),
            "cpu_time_seconds": round(cpu_time_seconds, 6),
        }
        if fds is not None:
            sample.update(
                {
                    "fds": fds["total"],
                    "sockets": fds["sockets"],
                    "pipes": fds["pipes"],
                    "inotify_fds": fds["inotify_fds"],
                    "inotify_watches": fds["inotify_watches"],
                }
            )
        else:
            sample.update(
                {
                    "fds": None,
                    "sockets": None,
                    "pipes": None,
                    "inotify_fds": None,
                    "inotify_watches": None,
                }
            )
        return sample

    @staticmethod
    def _read_process_stat(process_root: Path) -> dict[str, int] | None:
        """Lê campos estáveis de /proc/<pid>/stat sem quebrar nomes com espaços."""
        try:
            raw = (process_root / "stat").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        closing_paren = raw.rfind(")")
        if closing_paren < 0:
            return None
        fields = raw[closing_paren + 1 :].strip().split()
        # fields[0] corresponde ao campo 3 (state); starttime é o campo 22.
        if len(fields) < 20:
            return None
        try:
            return {
                "utime_ticks": int(fields[11]),
                "stime_ticks": int(fields[12]),
                "start_ticks": int(fields[19]),
            }
        except (TypeError, ValueError):
            return None

    def _is_cancelled(self, call: ToolCall) -> bool:
        metadata = call.metadata if isinstance(call.metadata, dict) else {}
        event = metadata.get("_mcp_cancel_event")
        is_set = getattr(event, "is_set", None)
        if callable(is_set):
            try:
                if bool(is_set()):
                    return True
            except Exception:  # noqa: BLE001 - cancel source is external plumbing
                is_set = None
        if self._cancel_checker is not None:
            try:
                return bool(self._cancel_checker())
            except Exception:  # noqa: BLE001 - cancellation must not break diagnostics
                return False
        return False

    def _wait_cooperatively(self, call: ToolCall, delay_seconds: float) -> bool:
        deadline = self._monotonic() + max(0.0, delay_seconds)
        while True:
            if self._is_cancelled(call):
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._sleep(min(0.1, remaining))

    def _report_sample_progress(
        self,
        pid: int,
        sample: dict[str, Any],
        *,
        current: int,
        total: int,
    ) -> None:
        callback = self._progress_callback
        if callback is None:
            return
        fds = sample.get("fds")
        watches = sample.get("inotify_watches")
        suffix = ""
        if fds is not None:
            suffix = f" fds={fds} inotify={watches}"
        try:
            callback(
                f"host sample pid={pid} {current}/{total} "
                f"rss={sample['rss_kb']}kB threads={sample['threads']}{suffix}"
            )
        except Exception:  # noqa: BLE001 - progress is best-effort
            return

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

    def _validate_host_process_sample(self, call: ToolCall) -> None:
        if "pid" not in call.arguments:
            raise ToolPolicyError("host_process_sample requer 'pid'")
        try:
            pid = int(call.arguments["pid"])
        except (TypeError, ValueError) as exc:
            raise ToolPolicyError("host_process_sample.pid deve ser inteiro positivo") from exc
        if pid <= 0:
            raise ToolPolicyError("host_process_sample.pid deve ser inteiro positivo")

        try:
            duration_seconds = float(call.arguments.get("duration_seconds", 5.0))
        except (TypeError, ValueError) as exc:
            raise ToolPolicyError(
                "host_process_sample.duration_seconds deve ser um número"
            ) from exc
        if not 0.5 <= duration_seconds <= 60.0:
            raise ToolPolicyError(
                "host_process_sample.duration_seconds deve estar entre 0.5 e 60"
            )

        try:
            interval_ms = int(call.arguments.get("interval_ms", 500))
        except (TypeError, ValueError) as exc:
            raise ToolPolicyError("host_process_sample.interval_ms deve ser inteiro") from exc
        if not 100 <= interval_ms <= 5000:
            raise ToolPolicyError(
                "host_process_sample.interval_ms deve estar entre 100 e 5000"
            )
        if interval_ms > duration_seconds * 1000:
            raise ToolPolicyError(
                "host_process_sample.interval_ms não pode exceder a duração total"
            )
        sample_count = ceil(duration_seconds / (interval_ms / 1000.0)) + 1
        if sample_count > _MAX_PROCESS_SAMPLES:
            raise ToolPolicyError(
                f"host_process_sample aceita no máximo {_MAX_PROCESS_SAMPLES} amostras; "
                "aumente interval_ms ou reduza duration_seconds"
            )
        include_fds = call.arguments.get("include_fds", True)
        if not isinstance(include_fds, bool):
            raise ToolPolicyError("host_process_sample.include_fds deve ser booleano")

    def _validate_host_memory(self, call: ToolCall) -> None:
        return


def register(registry, policy, config) -> HostTools:
    """Registra as tools read-only de diagnóstico do host."""
    tools = HostTools(config)
    validator = HostToolsValidator(config)
    for name in _HOST_TOOL_NAMES:
        registry.register(name, getattr(tools, name))
    policy.register_tool_validator(_HOST_TOOL_NAMES, validator)
    return tools
