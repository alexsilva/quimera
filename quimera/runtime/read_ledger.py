"""Ledger thread-safe de arquivos lidos por run antes de mutacoes.

O ledger reduz a janela de edicoes cegas em workspaces compartilhados. Ele nao
faz locking de arquivos: registra a identidade observada em uma leitura e
permite que as tools detectem se o arquivo mudou antes de uma mutacao.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import threading
import time
from typing import Any

from .models import ToolCall


DEFAULT_MAX_LEDGER_SCOPES = 64
DEFAULT_MAX_LEDGER_PATHS_PER_SCOPE = 512


class LedgerVerdict(str, Enum):
    """Resultado da comparacao entre a ultima leitura e o arquivo atual."""

    OK = "ok"
    NOT_READ = "not_read"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    """Identidade conservadora de um arquivo num instante especifico."""

    mtime_ns: int
    ctime_ns: int
    size: int
    device: int
    inode: int

    @classmethod
    def from_stat(cls, stat: os.stat_result) -> FileFingerprint:
        """Constroi o fingerprint de um ``stat`` ou ``fstat`` ja capturado."""
        return cls(
            mtime_ns=stat.st_mtime_ns,
            ctime_ns=stat.st_ctime_ns,
            size=stat.st_size,
            device=stat.st_dev,
            inode=stat.st_ino,
        )

    @classmethod
    def from_path(cls, path: Path) -> FileFingerprint:
        """Constroi o fingerprint a partir de um unico ``stat``."""
        return cls.from_stat(path.stat())


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Leitura registrada e seu instante de observacao."""

    fingerprint: FileFingerprint
    read_at: float

    @property
    def mtime_ns(self) -> int:
        """Mantem acesso direto ao campo mais util para telemetria."""
        return self.fingerprint.mtime_ns

    @property
    def ctime_ns(self) -> int:
        """Retorna o ctime observado."""
        return self.fingerprint.ctime_ns

    @property
    def size(self) -> int:
        """Retorna o tamanho observado."""
        return self.fingerprint.size

    @property
    def device(self) -> int:
        """Retorna o device observado."""
        return self.fingerprint.device

    @property
    def inode(self) -> int:
        """Retorna o inode observado."""
        return self.fingerprint.inode


@dataclass(frozen=True, slots=True)
class LedgerInspection:
    """Diagnostico completo usado para montar erros estruturados."""

    verdict: LedgerVerdict
    entry: LedgerEntry | None
    current: FileFingerprint | None


@dataclass(frozen=True, slots=True)
class ReadGuardDecision:
    """Mensagem e dados estáveis para tools que exigem leitura prévia."""

    verdict: LedgerVerdict
    message: str
    data: dict[str, object]

    @property
    def allowed(self) -> bool:
        return self.verdict is LedgerVerdict.OK


class ReadLedger:
    """Registro LRU de fingerprints, particionado por run confiavel."""

    def __init__(
        self,
        *,
        max_scopes: int = DEFAULT_MAX_LEDGER_SCOPES,
        max_paths_per_scope: int = DEFAULT_MAX_LEDGER_PATHS_PER_SCOPE,
    ) -> None:
        for name, value in (
            ("max_scopes", max_scopes),
            ("max_paths_per_scope", max_paths_per_scope),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} deve ser um inteiro positivo")
        self.max_scopes = max_scopes
        self.max_paths_per_scope = max_paths_per_scope
        self._scopes: OrderedDict[str, OrderedDict[str, LedgerEntry]] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def _scope_key(scope: str) -> str:
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("scope deve ser uma string nao vazia")
        return scope.strip()

    @staticmethod
    def _path_key(path: Path) -> str:
        return str(Path(path).resolve(strict=False))

    def record(
        self,
        scope: str,
        path: Path,
        *,
        fingerprint: FileFingerprint | None = None,
    ) -> LedgerEntry:
        """Registra o estado atual de ``path`` como lido em ``scope``."""
        scope_key = self._scope_key(scope)
        resolved = Path(path).resolve(strict=False)
        entry = LedgerEntry(
            fingerprint=fingerprint or FileFingerprint.from_path(resolved),
            read_at=time.time(),
        )
        path_key = str(resolved)

        with self._lock:
            paths = self._scopes.get(scope_key)
            if paths is None:
                paths = OrderedDict()
                self._scopes[scope_key] = paths
            else:
                self._scopes.move_to_end(scope_key)
            paths[path_key] = entry
            paths.move_to_end(path_key)
            while len(paths) > self.max_paths_per_scope:
                paths.popitem(last=False)
            while len(self._scopes) > self.max_scopes:
                self._scopes.popitem(last=False)
        return entry

    def inspect(self, scope: str, path: Path) -> LedgerInspection:
        """Compara o arquivo atual com a leitura registrada, se houver."""
        scope_key = self._scope_key(scope)
        path_key = self._path_key(path)
        with self._lock:
            paths = self._scopes.get(scope_key)
            if paths is None:
                return LedgerInspection(LedgerVerdict.NOT_READ, None, None)
            entry = paths.get(path_key)
            if entry is None:
                return LedgerInspection(LedgerVerdict.NOT_READ, None, None)

            self._scopes.move_to_end(scope_key)
            paths.move_to_end(path_key)
            try:
                current = FileFingerprint.from_path(Path(path_key))
            except (FileNotFoundError, NotADirectoryError):
                current = None
            verdict = (
                LedgerVerdict.OK
                if current == entry.fingerprint
                else LedgerVerdict.STALE
            )
            return LedgerInspection(verdict, entry, current)

    def check(self, scope: str, path: Path) -> LedgerVerdict:
        """Retorna somente o veredito da inspecao."""
        return self.inspect(scope, path).verdict

    def get_entry(self, scope: str, path: Path) -> LedgerEntry | None:
        """Retorna a entrada registrada sem consultar novamente o arquivo."""
        scope_key = self._scope_key(scope)
        path_key = self._path_key(path)
        with self._lock:
            paths = self._scopes.get(scope_key)
            if paths is None:
                return None
            entry = paths.get(path_key)
            if entry is not None:
                self._scopes.move_to_end(scope_key)
                paths.move_to_end(path_key)
            return entry

    def forget(self, scope: str, path: Path) -> bool:
        """Remove um path do escopo, retornando se havia uma entrada."""
        scope_key = self._scope_key(scope)
        path_key = self._path_key(path)
        with self._lock:
            paths = self._scopes.get(scope_key)
            if paths is None:
                return False
            removed = paths.pop(path_key, None) is not None
            if not paths:
                self._scopes.pop(scope_key, None)
            return removed

    def drop_scope(self, scope: str) -> int:
        """Descarta um run e retorna quantos paths foram removidos."""
        scope_key = self._scope_key(scope)
        with self._lock:
            paths = self._scopes.pop(scope_key, None)
            return len(paths) if paths is not None else 0

    def clear(self) -> None:
        """Descarta todas as leituras, principalmente para isolamento de testes."""
        with self._lock:
            self._scopes.clear()

    @property
    def scope_count(self) -> int:
        """Quantidade atual de escopos, util para diagnostico e testes."""
        with self._lock:
            return len(self._scopes)

    def path_count(self, scope: str) -> int:
        """Quantidade atual de paths num escopo."""
        scope_key = self._scope_key(scope)
        with self._lock:
            return len(self._scopes.get(scope_key, ()))


def _context_value(context: Any, name: str) -> Any:
    if isinstance(context, dict):
        return context.get(name)
    return getattr(context, name, None)


def _clean_scope_part(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def ledger_scope_from_call(call: ToolCall) -> str:
    """Deriva o escopo do contexto confiavel, com fallback estavel por agente."""
    metadata = call.metadata if isinstance(call.metadata, dict) else {}
    context = metadata.get("trusted_context") or metadata.get("_trusted_context")
    run_id = _clean_scope_part(_context_value(context, "run_id"))
    if run_id:
        return run_id

    mcp_state = metadata.get("_mcp_state")
    if isinstance(mcp_state, dict):
        trusted_run_id = _clean_scope_part(mcp_state.get("trusted_run_id"))
        if trusted_run_id:
            return trusted_run_id

    agent = _clean_scope_part(_context_value(context, "agent_name"))
    if agent is None:
        agent = _clean_scope_part(metadata.get("calling_agent"))
    normalized_agent = (agent or "native").lower().lstrip("/")
    return f"fallback:{normalized_agent or 'native'}"


def evaluate_read_guard(
    call: ToolCall,
    path: Path,
    *,
    display_path: str | None = None,
) -> ReadGuardDecision:
    """Avalia a leitura prévia e produz diagnóstico comum a todas as tools."""
    inspection = get_read_ledger().inspect(ledger_scope_from_call(call), path)
    shown = display_path or str(path)
    if inspection.verdict is LedgerVerdict.OK:
        return ReadGuardDecision(LedgerVerdict.OK, "", {})
    if inspection.verdict is LedgerVerdict.NOT_READ:
        message = (
            f"{call.name}: '{shown}' não foi lido neste run. "
            "Use read_file antes de editar."
        )
    else:
        message = (
            f"{call.name}: '{shown}' mudou desde sua última leitura "
            "(possivelmente editado por outro agente). Releia com read_file "
            "e refaça a edição sobre o conteúdo atual."
        )

    data: dict[str, object] = {
        "path": str(path),
        "reason": inspection.verdict.value,
    }
    if inspection.entry is not None:
        read = inspection.entry.fingerprint
        data.update({
            "read_mtime_ns": read.mtime_ns,
            "read_ctime_ns": read.ctime_ns,
            "read_size": read.size,
            "read_device": read.device,
            "read_inode": read.inode,
        })
    if inspection.current is not None:
        current = inspection.current
        data.update({
            "current_mtime_ns": current.mtime_ns,
            "current_ctime_ns": current.ctime_ns,
            "current_size": current.size,
            "current_device": current.device,
            "current_inode": current.inode,
        })
    return ReadGuardDecision(inspection.verdict, message, data)


_READ_LEDGER = ReadLedger()


def get_read_ledger() -> ReadLedger:
    """Retorna o ledger compartilhado pelo runtime do processo."""
    return _READ_LEDGER


def reset_read_ledger() -> None:
    """Limpa o singleton sem invalidar referencias ja obtidas por tools."""
    _READ_LEDGER.clear()
