"""Identidade da execução de agente corrente, propagada pelo contexto.

Execuções simultâneas do mesmo agente (chat, task de workflow, delegação)
compartilham o nome do agente; só o ``run_id`` as distingue. Boa parte dos
eventos visuais nasce longe do gateway (driver, presenter, executor de tools)
e chega ao renderer apenas com o nome do agente. Este módulo guarda, em
``contextvars``, qual execução está rodando no fluxo corrente, para que o
renderer atribua cada evento à execução certa sem depender de um "run atual
por agente" global.

Threads novas não herdam o contexto: quem dispara trabalho da execução em
outra thread deve usar :func:`run_in_current_context`.
"""
from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeVar

_T = TypeVar("_T")


@dataclass(frozen=True)
class AgentRunScope:
    """Identidade visual de uma execução de agente."""

    agent: str
    run_id: str
    parent_run_id: str = ""
    delegation_id: str = ""
    transport: str = ""

    def as_payload(self) -> dict[str, str]:
        """Campos de identidade não vazios, no formato dos payloads de UI."""
        return {
            key: value
            for key, value in (
                ("run_id", self.run_id),
                ("parent_run_id", self.parent_run_id),
                ("delegation_id", self.delegation_id),
                ("transport", self.transport),
            )
            if value
        }


_EMPTY_RECENT: Mapping[str, AgentRunScope] = MappingProxyType({})

_ACTIVE_SCOPE: contextvars.ContextVar[AgentRunScope | None] = contextvars.ContextVar(
    "quimera_active_agent_run_scope",
    default=None,
)
# Última execução encerrada de cada agente neste contexto. A resposta final é
# exibida depois que a chamada ao backend retorna, já fora do escopo ativo.
_RECENT_SCOPES: contextvars.ContextVar[Mapping[str, AgentRunScope]] = contextvars.ContextVar(
    "quimera_recent_agent_run_scopes",
    default=_EMPTY_RECENT,
)


def _agent_key(agent: Any) -> str:
    return str(agent or "").strip()


@contextmanager
def bind_agent_run_scope(scope: AgentRunScope | None, *, remember: bool = True) -> Iterator[None]:
    """Marca ``scope`` como a execução corrente enquanto o bloco roda.

    Ao sair, a execução vira a "recente" do agente neste contexto, inclusive
    quando o bloco termina com exceção. ``remember=False`` serve a quem só
    reapresenta um evento da execução numa thread alheia a ela (fila de UI).
    """
    if not isinstance(scope, AgentRunScope) or not scope.run_id:
        yield
        return
    token = _ACTIVE_SCOPE.set(scope)
    try:
        yield
    finally:
        _ACTIVE_SCOPE.reset(token)
        if remember:
            recent = dict(_RECENT_SCOPES.get())
            recent[_agent_key(scope.agent)] = scope
            _RECENT_SCOPES.set(MappingProxyType(recent))


def current_agent_run_scope(agent: Any) -> AgentRunScope | None:
    """Execução de ``agent`` associada ao contexto corrente, se houver.

    A ativa vence; sem ela, vale a última encerrada neste mesmo contexto.
    """
    key = _agent_key(agent)
    if not key:
        return None
    active = _ACTIVE_SCOPE.get()
    if active is not None and _agent_key(active.agent) == key:
        return active
    return _RECENT_SCOPES.get().get(key)


def run_in_current_context(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Envolve ``fn`` para rodar numa cópia do contexto corrente.

    A cópia é tirada agora, na thread que dispara o trabalho; cada chamada
    precisa do seu próprio wrapper porque um ``Context`` não pode ser
    executado por duas threads ao mesmo tempo.
    """
    context = contextvars.copy_context()

    def _runner(*args: Any, **kwargs: Any) -> _T:
        return context.run(fn, *args, **kwargs)

    return _runner


# Prefixo dos ids gerados para execuções silenciosas sem identidade própria.
# Esses ids nunca são reaproveitados por execução visível, então seguem
# reconhecidos como silenciosos mesmo depois do bloco: a thread de um driver
# abandonado por timeout/cancelamento ainda pode iniciar tools com eles.
SILENT_RUN_ID_PREFIX = "silent:"

# Execuções silenciosas em andamento, por ``run_id``. O executor de tools e o
# servidor MCP são compartilhados entre clients e threads; só a identidade da
# execução (nunca o nome do agente) distingue a silenciosa da visível. Conta
# entradas: um bloco aninhado com o mesmo id não desfaz a marca do externo.
_SILENT_RUN_IDS: dict[str, int] = {}
_SILENT_RUN_IDS_LOCK = threading.Lock()


@contextmanager
def silent_run(run_id: str | None) -> Iterator[None]:
    """Marca ``run_id`` como execução silenciosa enquanto o bloco roda."""
    key = str(run_id or "").strip()
    if not key:
        yield
        return
    with _SILENT_RUN_IDS_LOCK:
        _SILENT_RUN_IDS[key] = _SILENT_RUN_IDS.get(key, 0) + 1
    try:
        yield
    finally:
        with _SILENT_RUN_IDS_LOCK:
            remaining = _SILENT_RUN_IDS.get(key, 0) - 1
            if remaining > 0:
                _SILENT_RUN_IDS[key] = remaining
            else:
                _SILENT_RUN_IDS.pop(key, None)


def is_silent_run(run_id: Any) -> bool:
    """Indica se ``run_id`` pertence a uma execução silenciosa em andamento."""
    key = str(run_id or "").strip()
    if not key:
        return False
    if key.startswith(SILENT_RUN_ID_PREFIX):
        return True
    with _SILENT_RUN_IDS_LOCK:
        return key in _SILENT_RUN_IDS
