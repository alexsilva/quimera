"""Componentes de `quimera.runtime.tools.shell`."""
from __future__ import annotations
from collections import deque

import json
import os
import pty
import subprocess
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from . import files as file_tools
from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult


_MAX_CHUNK_CHARS = 50_000  # limite de caracteres por stream (stdout/stderr)
_MAX_SESSIONS = 64  # limite de sessões simultâneas


@dataclass
class CommandSession:
    """Representa um processo interativo ainda acessível por session_id."""

    session_id: int
    process: subprocess.Popen
    command: str
    cwd: Path
    started_at: float
    stdout_chunks: deque[str] = field(default_factory=deque)
    stderr_chunks: deque[str] = field(default_factory=deque)
    stdout_offset: int = 0
    stderr_offset: int = 0
    tty: bool = False
    tty_master_fd: int | None = None
    _stdout_total: int = 0
    _stderr_total: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class ShellTool:
    """Implementa `ShellTool`."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de ShellTool."""
        self.config = config
        self._sessions: dict[int, CommandSession] = {}
        self._next_session_id = 1
        self._sessions_lock = threading.Lock()

    def _enforce_session_limit(self) -> None:
        """Remove a sessão mais antiga se o limite for excedido."""
        with self._sessions_lock:
            while len(self._sessions) > _MAX_SESSIONS:
                oldest_id = min(self._sessions.keys())
                self._cleanup_session(oldest_id)

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
                "diff": self._build_output_diff(
                    stdout[: self.config.max_output_chars],
                    stderr[: self.config.max_output_chars],
                    completed=True,
                ),
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

    def exec_command(self, call: ToolCall) -> ToolResult:
        """Executa um comando com suporte a sessão persistente e polling incremental."""
        staging = file_tools.get_staging_root()
        if staging:
            warnings.warn(
                f"exec_command called in parallel mode with staging - cwd={self.config.workspace_root}, "
                f"staging={staging}. Shell writes bypass staging isolation.",
                UserWarning,
                stacklevel=2,
            )

        command = str(call.arguments["cmd"])
        workdir = self._resolve_workdir(call.arguments.get("workdir"))
        yield_time_ms = self._resolve_yield_time(call.arguments.get("yield_time_ms"))
        shell = str(call.arguments.get("shell") or os.environ.get("SHELL") or "/bin/bash")
        login = bool(call.arguments.get("login", True))
        tty_enabled = bool(call.arguments.get("tty", False))

        process, tty_master_fd = self._spawn_process(command, workdir, shell=shell, login=login, tty=tty_enabled)
        session = self._create_session(
            process,
            command=command,
            cwd=workdir,
            tty=tty_enabled,
            tty_master_fd=tty_master_fd,
        )
        self._start_reader_threads(session)
        return self._collect_session_result(
            session,
            yield_time_ms=yield_time_ms,
            tool_name=call.name,
            include_session_id=True,
            wait_for_completion=tty_enabled,
        )

    def write_stdin(self, call: ToolCall) -> ToolResult:
        """Escreve no stdin de uma sessão ativa e devolve saída incremental."""
        session_id = int(call.arguments["session_id"])
        session = self._sessions.get(session_id)
        if session is None:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=f"Sessão não encontrada: {session_id}",
            )

        chars = call.arguments.get("chars", "")
        if chars is None:
            chars = ""
        close_stdin = bool(call.arguments.get("close_stdin", False))
        yield_time_ms = self._resolve_yield_time(call.arguments.get("yield_time_ms"))

        try:
            if chars:
                self._write_to_session(session, str(chars))
            if close_stdin:
                self._close_session_stdin(session)
        except BrokenPipeError:
            close_stdin = True
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=f"Falha ao escrever na sessão {session_id}: {exc}",
            )

        return self._collect_session_result(
            session,
            yield_time_ms=yield_time_ms,
            tool_name=call.name,
            include_session_id=True,
            wait_for_completion=close_stdin,
        )

    def close_command_session(self, call: ToolCall) -> ToolResult:
        """Fecha explicitamente uma sessão ativa de comando."""
        session_id = int(call.arguments["session_id"])
        session = self._sessions.get(session_id)
        if session is None:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=f"Sessão não encontrada: {session_id}",
            )

        terminate = bool(call.arguments.get("terminate", True))
        try:
            if terminate and session.process.poll() is None:
                session.process.terminate()
                try:
                    session.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    session.process.kill()
            self._close_session_stdin(session)
            stdout, stderr = self._drain_session_output(session)
        finally:
            self._cleanup_session(session_id)

        payload = {
            "session_id": session_id,
            "command": session.command,
            "cwd": str(session.cwd),
            "stdout": stdout[: self.config.max_output_chars],
            "stderr": stderr[: self.config.max_output_chars],
            "status": "closed",
            "diff": self._build_output_diff(
                stdout[: self.config.max_output_chars],
                stderr[: self.config.max_output_chars],
                completed=True,
            ),
        }
        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            exit_code=session.process.poll(),
            duration_ms=int((time.perf_counter() - session.started_at) * 1000),
            truncated=len(stdout) > self.config.max_output_chars or len(stderr) > self.config.max_output_chars,
            data=payload,
        )

    def _resolve_workdir(self, raw_workdir: str | None) -> Path:
        """Resolve o diretório de trabalho do comando dentro da workspace."""
        if not raw_workdir:
            return self.config.workspace_root
        path = Path(raw_workdir)
        if not path.is_absolute():
            path = self.config.workspace_root / path
        return path.resolve()

    def _resolve_yield_time(self, raw_value) -> int:
        """Normaliza o tempo de espera antes de retornar saída parcial."""
        if raw_value is None:
            return self.config.interactive_command_default_yield_ms
        return max(0, int(raw_value))

    def _spawn_process(
            self,
            command: str,
            workdir: Path,
            *,
            shell: str,
            login: bool,
            tty: bool,
    ) -> tuple[subprocess.Popen, int | None]:
        """Cria o subprocesso usado por exec_command."""
        shell_args = [shell, "-lc" if login else "-c", command]
        if tty:
            master_fd, slave_fd = pty.openpty()
            process = subprocess.Popen(
                shell_args,
                cwd=workdir,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
            return process, master_fd
        process = subprocess.Popen(
            shell_args,
            cwd=workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return process, None

    def _create_session(
            self,
            process: subprocess.Popen,
            *,
            command: str,
            cwd: Path,
            tty: bool,
            tty_master_fd: int | None,
    ) -> CommandSession:
        """Registra uma nova sessão interativa e devolve seu estado."""
        with self._sessions_lock:
            session_id = self._next_session_id
            self._next_session_id += 1
        session = CommandSession(
            session_id=session_id,
            process=process,
            command=command,
            cwd=cwd,
            started_at=time.perf_counter(),
            tty=tty,
            tty_master_fd=tty_master_fd,
        )
        self._sessions[session_id] = session
        return session

    @staticmethod
    def _append_chunk(session: CommandSession, target: deque, counter_attr: str, chunk: str) -> None:
        """Append com limite de caracteres total por stream."""
        current_total = getattr(session, counter_attr)
        if current_total >= _MAX_CHUNK_CHARS:
            return  # descarta chunks além do limite
        remaining = _MAX_CHUNK_CHARS - current_total
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        target.append(chunk)
        setattr(session, counter_attr, current_total + len(chunk))

    @staticmethod
    def _reset_stream_counter(session: CommandSession) -> None:
        """Recalcula contadores a partir dos chunks atuais."""
        session._stdout_total = sum(len(c) for c in session.stdout_chunks)
        session._stderr_total = sum(len(c) for c in session.stderr_chunks)

    def _start_reader_threads(self, session: CommandSession) -> None:
        """Inicia leitores assíncronos de stdout e stderr da sessão."""
        if session.tty and session.tty_master_fd is not None:
            def _tty_reader() -> None:
                try:
                    while True:
                        try:
                            chunk = os.read(session.tty_master_fd, 4096)
                        except OSError:
                            break
                        if not chunk:
                            break
                        with session.lock:
                            decoded = chunk.decode(errors="replace")
                            self._append_chunk(session, session.stdout_chunks, "_stdout_total", decoded)
                            self._reset_stream_counter(session)
                finally:
                    if session.tty_master_fd is not None:
                        try:
                            os.close(session.tty_master_fd)
                        except OSError:
                            pass
                        session.tty_master_fd = None

            threading.Thread(target=_tty_reader, daemon=True).start()
            return

        def _reader(stream, target: deque, counter_attr: str) -> None:
            try:
                if stream is None:
                    return
                for raw in iter(stream.readline, ""):
                    with session.lock:
                        self._append_chunk(session, target, counter_attr, raw)
                        self._reset_stream_counter(session)
            finally:
                if stream is not None:
                    stream.close()

        threading.Thread(
            target=_reader,
            args=(session.process.stdout, session.stdout_chunks, "_stdout_total"),
            daemon=True,
        ).start()
        threading.Thread(
            target=_reader,
            args=(session.process.stderr, session.stderr_chunks, "_stderr_total"),
            daemon=True,
        ).start()

    def _collect_session_result(
            self,
            session: CommandSession,
            *,
            yield_time_ms: int,
            tool_name: str,
            include_session_id: bool,
            wait_for_completion: bool = False,
    ) -> ToolResult:
        """Coleta a saída incremental de uma sessão e devolve o estado atual."""
        wait_budget_ms = yield_time_ms if wait_for_completion else max(yield_time_ms, 100)
        deadline = time.perf_counter() + (wait_budget_ms / 1000)
        while session.process.poll() is None and time.perf_counter() < deadline:
            if not wait_for_completion and self._has_unread_output(session):
                break
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))

        stdout, stderr = self._drain_session_output(session)
        duration_ms = int((time.perf_counter() - session.started_at) * 1000)
        returncode = session.process.poll()
        if returncode is None and (stdout or stderr):
            grace_deadline = time.perf_counter() + 0.05
            while time.perf_counter() < grace_deadline:
                returncode = session.process.poll()
                if returncode is not None:
                    break
                time.sleep(0.005)
        completed = returncode is not None
        full_stdout, full_stderr = self._snapshot_session_output(session)
        payload = {
            "command": session.command,
            "cwd": str(session.cwd),
            "stdout": (full_stdout if completed else stdout)[: self.config.max_output_chars],
            "stderr": (full_stderr if completed else stderr)[: self.config.max_output_chars],
            "status": "completed" if completed else "running",
            "diff": self._build_output_diff(
                stdout[: self.config.max_output_chars],
                stderr[: self.config.max_output_chars],
                full_stdout=full_stdout[: self.config.max_output_chars],
                full_stderr=full_stderr[: self.config.max_output_chars],
                completed=completed,
            ),
        }
        if include_session_id and not completed:
            payload["session_id"] = session.session_id
        elif include_session_id and completed:
            payload["session_id"] = session.session_id

        visible_stdout = full_stdout if completed else stdout
        visible_stderr = full_stderr if completed else stderr
        truncated = (
            len(visible_stdout) > self.config.max_output_chars
            or len(visible_stderr) > self.config.max_output_chars
        )
        result = ToolResult(
            ok=(returncode == 0) if completed else True,
            tool_name=tool_name,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            exit_code=returncode,
            duration_ms=duration_ms,
            truncated=truncated,
            data=payload,
        )
        if completed:
            self._cleanup_session(session.session_id)
        return result

    def _truncate_consumed_chunks(self, session: CommandSession) -> None:
        """Remove chunks já consumidos para liberar memória."""
        min_offset = min(session.stdout_offset, session.stderr_offset)
        if min_offset > 0:
            for _ in range(min_offset):
                if session.stdout_chunks:
                    session.stdout_chunks.popleft()
                if session.stderr_chunks:
                    session.stderr_chunks.popleft()
            session.stdout_offset -= min_offset
            session.stderr_offset -= min_offset
            session._stdout_total = sum(len(c) for c in session.stdout_chunks)
            session._stderr_total = sum(len(c) for c in session.stderr_chunks)

    def _drain_session_output(self, session: CommandSession) -> tuple[str, str]:
        """Retorna apenas a saída nova desde a última leitura da sessão."""
        with session.lock:
            self._truncate_consumed_chunks(session)
            stdout = "".join(session.stdout_chunks[session.stdout_offset:])
            stderr = "".join(session.stderr_chunks[session.stderr_offset:])
            session.stdout_offset = len(session.stdout_chunks)
            session.stderr_offset = len(session.stderr_chunks)
        return stdout, stderr

    def _snapshot_session_output(self, session: CommandSession) -> tuple[str, str]:
        """Retorna toda a saída acumulada da sessão sem alterar offsets."""
        with session.lock:
            self._truncate_consumed_chunks(session)
            return "".join(session.stdout_chunks), "".join(session.stderr_chunks)

    def _has_unread_output(self, session: CommandSession) -> bool:
        """Indica se há saída ainda não entregue para o consumidor."""
        with session.lock:
            return (
                session.stdout_offset < len(session.stdout_chunks)
                or session.stderr_offset < len(session.stderr_chunks)
            )

    def _cleanup_session(self, session_id: int) -> None:
        """Remove uma sessão concluída do registro interno."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        if session.tty_master_fd is not None:
            try:
                os.close(session.tty_master_fd)
            except OSError:
                pass
            session.tty_master_fd = None

    def _write_to_session(self, session: CommandSession, chars: str) -> None:
        """Escreve dados no canal de entrada da sessão."""
        if session.tty:
            if session.tty_master_fd is None:
                raise BrokenPipeError("PTY master fechado")
            os.write(session.tty_master_fd, chars.encode())
            return
        if session.process.stdin is None:
            raise BrokenPipeError("stdin indisponível")
        session.process.stdin.write(chars)
        session.process.stdin.flush()

    def _close_session_stdin(self, session: CommandSession) -> None:
        """Fecha o canal de entrada da sessão."""
        if session.tty:
            if session.tty_master_fd is not None:
                try:
                    os.close(session.tty_master_fd)
                except OSError:
                    pass
                session.tty_master_fd = None
            return
        if session.process.stdin and not session.process.stdin.closed:
            session.process.stdin.close()

    @staticmethod
    def _build_output_diff(
        stdout: str,
        stderr: str,
        *,
        full_stdout: str | None = None,
        full_stderr: str | None = None,
        completed: bool,
    ) -> list[dict[str, str]]:
        """Representa saída incremental do shell em operações simples de UI."""
        if completed:
            combined = f"{full_stdout or ''}{full_stderr or ''}"
            return [{"op": "replace", "text": combined}] if combined else []
        combined = f"{stdout}{stderr}"
        return [{"op": "add", "text": combined}] if combined else []
