"""Componentes de `quimera.runtime.tools.shell`."""
from __future__ import annotations

import os
import pty
import random
import shlex
import re
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from quimera import process_factory as subprocess

from . import files as file_tools
from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError, is_path_inside
from .base import ToolBase, ValidatableTool


_MAX_CHUNK_CHARS = 250_000  # limite de caracteres por stream (stdout/stderr)
_MAX_SESSIONS = 64  # limite de sessões simultâneas
_DENYLIST_REGEX = re.compile(
    r"(?:^|\s)(?:"
    r"rm\s+-rf|"
    r"rm\s+-r\s+/|"
    r"sudo(?:\s+|$)|"
    r"systemctl|"
    r"shutdown|"
    r"reboot|"
    r"poweroff|"
    r"mkfs|"
    r":\s*\(\s*\{|"
    r":\s*\(\)\s*\{|"
    r"chmod\s+-R\s+777|"
    r"chown\s+-R|"
    r"chattr|"
    r"dd\s+if=|"
    r">\s*/dev/(?:sd|nvme|hd|xvd|vd|loop|mmcblk)[a-z0-9]*"
    r")(?:\s|$)"
)
_ALLOWED_SHELLS: frozenset[str] = frozenset({
    "/bin/bash", "/bin/sh", "/bin/zsh", "/bin/dash",
    "/usr/bin/bash", "/usr/bin/sh", "/usr/bin/zsh", "/usr/bin/dash",
})


@dataclass
class CommandSession:
    """Representa um processo interativo ainda acessível por session_id."""

    session_id: int
    process: subprocess.ProcessHandle
    command: str
    cwd: Path
    started_at: float
    stdout_buffer: str = ""
    stderr_buffer: str = ""
    stdout_history: str = ""
    stderr_history: str = ""
    stdout_offset: int = 0
    stderr_offset: int = 0
    tty: bool = False
    tty_master_fd: int | None = None
    _stdout_total: int = 0
    _stderr_total: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader_threads: list[threading.Thread] = field(default_factory=list)


class ShellTool(ToolBase):
    """Ferramentas de execução shell com suporte a sessões persistentes e polling incremental."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de ShellTool."""
        super().__init__(config)
        self._sessions: dict[int, CommandSession] = {}
        self._next_session_id = random.SystemRandom().randint(100000, 999999999)
        self._sessions_lock = threading.Lock()

    def _enforce_session_limit(self) -> None:
        """Remove a sessão mais antiga se o limite for excedido."""
        while True:
            with self._sessions_lock:
                if len(self._sessions) <= _MAX_SESSIONS:
                    return
                oldest_id = min(self._sessions, key=lambda sid: self._sessions[sid].started_at)
                session = self._sessions.pop(oldest_id, None)
            if session is not None:
                self._cleanup_session_resources(session, terminate=True)

    def run_shell(self, call: ToolCall) -> ToolResult:
        """Executa um comando shell único e retorna stdout/stderr com timeout."""
        staging = file_tools.get_staging_root()
        if staging:
            warnings.warn(
                f"run_shell called in parallel mode with staging - cwd={self.config.workspace_root}, "
                f"staging={staging}. Shell writes bypass staging isolation.",
                UserWarning,
                stacklevel=2,
            )

        command = str(call.arguments["command"])
        command = self._rewrite_command_for_local_venv(command, self.config.workspace_root)
        started = time.perf_counter()
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(self.config.workspace_root),
            capture_output=True,
            text=True,
            timeout=self.config.command_timeout_seconds,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        visible_stdout = stdout[: self.config.max_output_chars]
        visible_stderr = stderr[: self.config.max_output_chars]
        payload = {
            "command": command,
            "cwd": str(self.config.workspace_root),
            "stdout": visible_stdout,
            "stderr": visible_stderr,
            "diff": self._build_output_diff(
                visible_stdout,
                visible_stderr,
                completed=True,
            ),
        }
        truncated = len(stdout) > self.config.max_output_chars or len(stderr) > self.config.max_output_chars
        return ToolResult(
            ok=proc.returncode == 0,
            tool_name=call.name,
            content=self._format_shell_content(stdout=visible_stdout, stderr=visible_stderr),
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            truncated=truncated,
            data=payload,
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

        command = self._rewrite_command_for_local_venv(command, workdir)
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

    def poll_command_session(self, call: ToolCall) -> ToolResult:
        """Consulta a saída incremental de uma sessão sem escrever no stdin.

        Esta ferramenta cobre o caso de polling puro sem precisar enviar
        `chars=""` para `write_stdin`, o que evita bloqueios de segurança em
        clientes que tratam string vazia em tool-call como payload suspeito.
        """

        session_id = int(call.arguments["session_id"])
        session = self._sessions.get(session_id)
        if session is None:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=f"Sessão não encontrada: {session_id}",
            )
        yield_time_ms = self._resolve_yield_time(call.arguments.get("yield_time_ms"))
        wait_for_completion = bool(call.arguments.get("wait_for_completion", False))
        return self._collect_session_result(
            session,
            yield_time_ms=yield_time_ms,
            tool_name=call.name,
            include_session_id=True,
            wait_for_completion=wait_for_completion,
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
                    try:
                        session.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
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
            content=self._format_shell_content(
                stdout=payload["stdout"],
                stderr=payload["stderr"],
                session_id=session_id,
                status="closed",
            ),
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
        resolved = path.resolve()
        if not is_path_inside(resolved, self.config.workspace_root):
            raise ToolPolicyError(f"workdir fora da workspace: {raw_workdir}")
        return resolved

    def _resolve_yield_time(self, raw_value) -> int:
        """Normaliza o tempo de espera antes de retornar saída parcial."""
        if raw_value is None:
            return self.config.interactive_command_default_yield_ms
        return max(0, int(raw_value))

    def _rewrite_command_for_local_venv(self, command: str, workdir: Path) -> str:
        """Prefere executáveis do `.venv` da workspace alvo para comandos Python comuns.

        O processo do Quimera pode estar rodando dentro do próprio virtualenv do
        app. Sem esta reescrita, comandos como `pytest` e `python -m pytest`
        podem executar no venv do Quimera em vez do projeto aberto no `workdir`.
        A troca só ocorre para comandos simples, já validados sem chaining, e
        somente quando o executável correspondente existe em `workdir/.venv/bin`.
        """

        try:
            tokens = shlex.split(command)
        except ValueError:
            return command
        if not tokens:
            return command
        first_token = Path(tokens[0]).name
        if first_token not in {"python", "python3", "pytest", "pip"}:
            return command
        candidate = workdir / ".venv" / "bin" / first_token
        if not candidate.exists():
            if first_token == "python3":
                candidate = workdir / ".venv" / "bin" / "python"
            if not candidate.exists():
                return command
        tokens[0] = str(candidate)
        return shlex.join(tokens)

    def _spawn_process(
            self,
            command: str,
            workdir: Path,
            *,
            shell: str,
            login: bool,
            tty: bool,
    ) -> tuple[subprocess.ProcessHandle, int | None]:
        """Cria o subprocesso usado por exec_command."""
        shell_args = [shell, "-lc" if login else "-c", command]
        if tty:
            master_fd, slave_fd = pty.openpty()
            process = subprocess.Popen(
                shell_args,
                cwd=str(workdir),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
            return process, master_fd
        process = subprocess.popen_text(shell_args, cwd=str(workdir))
        return process, None

    def _create_session(
            self,
            process: subprocess.ProcessHandle,
            *,
            command: str,
            cwd: Path,
            tty: bool,
            tty_master_fd: int | None,
    ) -> CommandSession:
        """Registra uma nova sessão interativa e devolve seu estado."""
        _rand = random.SystemRandom()
        session: CommandSession
        with self._sessions_lock:
            session_id = _rand.randint(100000, 999999999)
            while session_id in self._sessions:
                session_id = _rand.randint(100000, 999999999)
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
        self._enforce_session_limit()
        return session

    @staticmethod
    def _append_chunk(
        session: CommandSession,
        buffer_attr: str,
        history_attr: str,
        counter_attr: str,
        chunk: str,
    ) -> None:
        """Append com limite de caracteres total por stream."""
        current_total = getattr(session, counter_attr)
        if current_total >= _MAX_CHUNK_CHARS:
            return  # descarta chunks além do limite
        remaining = _MAX_CHUNK_CHARS - current_total
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        setattr(session, buffer_attr, getattr(session, buffer_attr) + chunk)
        setattr(session, history_attr, getattr(session, history_attr) + chunk)
        setattr(session, counter_attr, current_total + len(chunk))

    @staticmethod
    def _reset_stream_counter(session: CommandSession) -> None:
        """Recalcula contadores a partir dos chunks atuais."""
        session._stdout_total = len(session.stdout_history)
        session._stderr_total = len(session.stderr_history)

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
                            self._append_chunk(
                                session,
                                "stdout_buffer",
                                "stdout_history",
                                "_stdout_total",
                                decoded,
                            )
                            self._reset_stream_counter(session)
                finally:
                    if session.tty_master_fd is not None:
                        try:
                            os.close(session.tty_master_fd)
                        except OSError:
                            pass
                        session.tty_master_fd = None

            thread = threading.Thread(target=_tty_reader, daemon=True)
            session.reader_threads.append(thread)
            thread.start()
            return

        def _reader(stream, buffer_attr: str, counter_attr: str) -> None:
            try:
                if stream is None:
                    return
                for raw in iter(stream.readline, ""):
                    with session.lock:
                        history_attr = "stdout_history" if buffer_attr == "stdout_buffer" else "stderr_history"
                        self._append_chunk(session, buffer_attr, history_attr, counter_attr, raw)
                        self._reset_stream_counter(session)
            finally:
                if stream is not None:
                    stream.close()

        stdout_thread = threading.Thread(
            target=_reader,
            args=(session.process.stdout, "stdout_buffer", "_stdout_total"),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_reader,
            args=(session.process.stderr, "stderr_buffer", "_stderr_total"),
            daemon=True,
        )
        session.reader_threads.extend([stdout_thread, stderr_thread])
        stdout_thread.start()
        stderr_thread.start()

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

        # PTY pode sofrer variações de agendamento (spawn + dispatch de thread)
        # e retornar "running" para comandos curtos imediatamente após o deadline.
        if wait_for_completion and session.process.poll() is None:
            try:
                completion_grace_s = max(0.5, min(1.5, (yield_time_ms / 1000) + 0.4))
                session.process.wait(timeout=completion_grace_s)
            except subprocess.TimeoutExpired:
                pass

        stdout, stderr = self._drain_session_output(session)

        # Se não capturamos nada e o processo ainda está rodando, espera um
        # pouco mais para dar chance à thread leitora (importante em ambientes
        # com maior latência de subprocesso como PyCharm).
        if not stdout and not stderr and session.process.poll() is None:
            extra_deadline = time.perf_counter() + 0.2
            while session.process.poll() is None and time.perf_counter() < extra_deadline:
                if self._has_unread_output(session):
                    break
                time.sleep(0.005)
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
            "closed": bool(completed),
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
            content=self._format_shell_content(
                stdout=payload["stdout"],
                stderr=payload["stderr"],
                session_id=session.session_id if include_session_id else None,
                status=payload["status"],
            ),
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
        # Trunca cada stream independentemente, não pelo min comum.
        # Se só stdout avança e stderr nunca recebe dados (offset=0),
        # o min() seria 0 e stdout acumularia sem limite.
        if session.stdout_offset > 0:
            session.stdout_buffer = session.stdout_buffer[session.stdout_offset:]
            session.stdout_offset = 0
        if session.stderr_offset > 0:
            session.stderr_buffer = session.stderr_buffer[session.stderr_offset:]
            session.stderr_offset = 0

    def _drain_session_output(self, session: CommandSession) -> tuple[str, str]:
        """Retorna apenas a saída nova desde a última leitura da sessão."""
        with session.lock:
            self._truncate_consumed_chunks(session)
            stdout = session.stdout_buffer[session.stdout_offset:]
            stderr = session.stderr_buffer[session.stderr_offset:]
            session.stdout_offset = len(session.stdout_buffer)
            session.stderr_offset = len(session.stderr_buffer)
        return stdout, stderr

    def _snapshot_session_output(self, session: CommandSession) -> tuple[str, str]:
        """Retorna toda a saída acumulada da sessão sem alterar offsets."""
        with session.lock:
            self._truncate_consumed_chunks(session)
            return session.stdout_history, session.stderr_history

    def _has_unread_output(self, session: CommandSession) -> bool:
        """Indica se há saída ainda não entregue para o consumidor."""
        with session.lock:
            return (
                session.stdout_offset < len(session.stdout_buffer)
                or session.stderr_offset < len(session.stderr_buffer)
            )

    def _cleanup_session(self, session_id: int) -> None:
        """Remove uma sessão concluída do registro interno."""
        with self._sessions_lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        # Join reader threads to ensure they finish before cleanup
        for thread in session.reader_threads:
            if thread.is_alive():
                thread.join(timeout=1.0)
        self._cleanup_session_resources(session)

    @staticmethod
    def _cleanup_session_resources(session: CommandSession, *, terminate: bool = False) -> None:
        """Libera recursos associados a uma sessão já removida do registro."""
        if terminate and session.process.poll() is None:
            session.process.terminate()
            try:
                session.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                session.process.kill()
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

    @staticmethod
    def _format_shell_content(
        *,
        stdout: str,
        stderr: str,
        session_id: int | None = None,
        status: str | None = None,
    ) -> str:
        """Monta conteúdo textual enxuto para o modelo."""
        parts: list[str] = []
        if session_id is not None:
            parts.append(f"session_id: {session_id}")
        if status:
            parts.append(f"status: {status}")
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        return "\n\n".join(parts)


class ShellToolValidator(ValidatableTool):
    """Validação de policy para as ferramentas shell."""

    _SHELL_CHAIN_OPERATORS = (";", "&&", "||", "|", "`", "$(")
    _SHELL_CHAIN_TOKENS = frozenset({";", "&&", "||", "|"})
    _SHELL_OPERATOR_PATTERN = re.compile(r"&&|\|\||[;|]")
    _FILE_PATH_CMDS = frozenset({"cat", "head", "tail", "less", "grep", "sed", "find", "ls"})
    _MKDIR_ALLOWED_FLAGS = frozenset({"-p", "--parents", "-v", "--verbose"})

    def _validate_run_shell(self, call: ToolCall) -> None:
        """Executa validate run shell."""
        command = str(call.arguments.get("command", "")).strip()
        self._validate_shell_command(command, tool_name="run_shell")

    def _validate_exec_command(self, call: ToolCall) -> None:
        """Valida uma chamada interativa de execução de comando."""
        command = str(call.arguments.get("cmd", "")).strip()
        self._validate_shell_command(command, tool_name="exec_command")
        raw_shell = call.arguments.get("shell")
        if raw_shell is not None and str(raw_shell) not in _ALLOWED_SHELLS:
            raise ToolPolicyError(f"Shell não permitido: {raw_shell}")
        raw_workdir = call.arguments.get("workdir")
        if raw_workdir is not None:
            self._resolve_workspace_path(str(raw_workdir))

    def _validate_write_stdin(self, call: ToolCall) -> None:
        """Valida uma operação de escrita ou polling em sessão ativa."""
        if "session_id" not in call.arguments:
            raise ToolPolicyError("write_stdin requer 'session_id'")
        try:
            int(call.arguments["session_id"])
        except (ValueError, TypeError) as exc:
            raise ToolPolicyError("write_stdin requer um session_id inteiro") from exc
        if "yield_time_ms" in call.arguments:
            try:
                int(call.arguments["yield_time_ms"])
            except (ValueError, TypeError) as exc:
                raise ToolPolicyError("write_stdin requer yield_time_ms inteiro") from exc

    def _validate_close_command_session(self, call: ToolCall) -> None:
        """Valida o fechamento explícito de uma sessão de comando."""
        if "session_id" not in call.arguments:
            raise ToolPolicyError("close_command_session requer 'session_id'")
        try:
            int(call.arguments["session_id"])
        except (ValueError, TypeError) as exc:
            raise ToolPolicyError("close_command_session requer um session_id inteiro") from exc

    def _validate_poll_command_session(self, call: ToolCall) -> None:
        """Valida uma consulta de saída incremental sem escrita em stdin."""
        if "session_id" not in call.arguments:
            raise ToolPolicyError("poll_command_session requer 'session_id'")
        try:
            int(call.arguments["session_id"])
        except (ValueError, TypeError) as exc:
            raise ToolPolicyError("poll_command_session requer um session_id inteiro") from exc
        if "yield_time_ms" in call.arguments:
            try:
                int(call.arguments["yield_time_ms"])
            except (ValueError, TypeError) as exc:
                raise ToolPolicyError("poll_command_session requer yield_time_ms inteiro") from exc

    def _validate_shell_command(self, command: str, *, tool_name: str) -> None:
        """Aplica a política comum de shell para ferramentas de comando."""
        if not command:
            raise ToolPolicyError(f"{tool_name} requer um comando não vazio")
        if "$IFS" in command:
            raise ToolPolicyError("Comando bloqueado: tentativa de bypass com $IFS")
        if _DENYLIST_REGEX.search(command):
            raise ToolPolicyError("Comando bloqueado pelo padrão de segurança")
        # Denylist sempre se aplica, mesmo em modo autônomo
        lowered = f" {command.lower()} "
        for pattern in self.config.shell_denylist_patterns:
            if pattern.lower() in lowered:
                raise ToolPolicyError(f"Comando bloqueado pela denylist: {pattern}")
        policy = self.config.workspace_policy
        chaining_allowed = policy is not None and policy.shell_allow_chaining
        allowlist_skipped = policy is not None and policy.shell_skip_allowlist
        try:
            tokens = shlex.split(command)
            first_token = tokens[0]
        except Exception as exc:  # noqa: BLE001
            raise ToolPolicyError(f"Comando inválido: {command}") from exc
        if not chaining_allowed:
            self._validate_shell_operators(command)
        if allowlist_skipped:
            return
        first_token = self._normalize_allowed_executable(first_token)
        if first_token not in self.config.shell_allowlist:
            raise ToolPolicyError(f"Comando fora da allowlist: {first_token}")
        if first_token == "git" and len(tokens) > 1 and tokens[1] in {"push"}:
            raise ToolPolicyError("Comando bloqueado: git push exige confirmação forte fora do shell MCP")
        if first_token in self._FILE_PATH_CMDS:
            self._validate_shell_file_paths(tokens[1:])
        if first_token == "mkdir":
            self._validate_mkdir_args(tokens[1:])

    def _normalize_allowed_executable(self, first_token: str) -> str:
        """Normaliza executáveis relativos/absolutos dentro da workspace para allowlist.

        Permite chamar caminhos como `.venv/bin/pytest` ou
        `/workspace/.venv/bin/python` quando o basename está na allowlist e o
        executável resolvido permanece dentro do workspace. Isso mantém o
        confinement de path sem obrigar o agente a depender do PATH herdado pelo
        processo do Quimera.
        """

        if "/" not in first_token:
            return first_token
        candidate = Path(first_token).expanduser()
        if not candidate.is_absolute():
            candidate = self.config.workspace_root / candidate
        resolved_parent = candidate.parent.resolve()
        if not is_path_inside(resolved_parent, self.config.workspace_root):
            raise ToolPolicyError(f"Executável fora do workspace: {first_token}")
        return candidate.name

    def _validate_shell_operators(self, command: str) -> None:
        """Bloqueia operadores reais de shell, mas ignora caracteres dentro de quotes.

        A validação anterior baseada em tokens de `shlex.split` deixava passar
        operadores grudados (`echo ok;cat`). Um scan lexical simples preserva
        a distinção necessária: operadores fora de aspas são bloqueados, mas um
        ponto-e-vírgula dentro de `python -c 'print(1); print(2)'` continua
        sendo apenas conteúdo do argumento.
        """

        quote: str | None = None
        escaped = False
        index = 0
        while index < len(command):
            char = command[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if char == "\\":
                escaped = True
                index += 1
                continue
            if quote is not None:
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in {"'", '"'}:
                quote = char
                index += 1
                continue
            if char == "`":
                raise ToolPolicyError("Comando bloqueado: operador de encadeamento proibido: '`'")
            if command.startswith("$(", index):
                raise ToolPolicyError("Comando bloqueado: operador de encadeamento proibido: '$('")
            match = self._SHELL_OPERATOR_PATTERN.match(command, index)
            if match:
                raise ToolPolicyError(
                    f"Comando bloqueado: operador de encadeamento proibido: '{match.group(0)}'"
                )
            index += 1

    def _validate_mkdir_args(self, args: list[str]) -> None:
        """Valida mkdir como mutação restrita ao workspace e sem flags perigosas."""
        paths: list[str] = []
        parse_flags = True
        for arg in args:
            if parse_flags and arg == "--":
                parse_flags = False
                continue
            if parse_flags and arg.startswith("-"):
                if arg not in self._MKDIR_ALLOWED_FLAGS:
                    raise ToolPolicyError(f"Flag não permitida para mkdir: {arg}")
                continue
            paths.append(arg)
        if not paths:
            raise ToolPolicyError("mkdir requer ao menos um diretório")
        for raw_path in paths:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = self.config.workspace_root / candidate
            resolved = candidate.resolve()
            if not is_path_inside(resolved, self.config.workspace_root):
                raise ToolPolicyError(f"Caminho fora do workspace: {raw_path}")

    def _validate_shell_file_paths(self, args: list[str]) -> None:
        """Valida que paths absolutos em argumentos de comandos de leitura ficam dentro do workspace."""
        for arg in args:
            expanded = Path(arg).expanduser()
            if not expanded.is_absolute():
                continue
            resolved = expanded.resolve()
            from ..policy import is_path_inside
            if not is_path_inside(resolved, self.config.workspace_root):
                raise ToolPolicyError(f"Caminho fora do workspace: {arg}")


def register(registry, policy, config) -> None:
    """Registra todas as tools shell no registry e a validação na policy."""
    shell_tool = ShellTool(config)
    shell_validator = ShellToolValidator(config)
    _SHELL_TOOL_NAMES = [
        'run_shell',
        'exec_command',
        'write_stdin',
        'poll_command_session',
        'close_command_session',
    ]
    for name in _SHELL_TOOL_NAMES:
        registry.register(name, getattr(shell_tool, name))
    policy.register_tool_validator(_SHELL_TOOL_NAMES, shell_validator)
