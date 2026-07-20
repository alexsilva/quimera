"""Acesso seguro às capabilities públicas de clients de agente.

Os helpers preferem propriedades/métodos declarados pela classe concreta e
mantêm fallback para os atributos privados históricos. Isso preserva plugins e
fakes antigos sem aceitar atributos fabricados dinamicamente por mocks.
"""
from __future__ import annotations

from typing import Any


_MISSING = object()


def _declared_descriptor(obj: Any, name: str):
    return getattr(type(obj), name, None) if obj is not None else None


def _explicit_instance_attr(obj: Any, name: str, default=None):
    """Lê somente atributos realmente armazenados na instância.

    ``Mock`` e proxies dinâmicos fabricam atributos via ``__getattr__``; usar
    ``vars`` no fallback impede que capabilities inexistentes sejam aceitas.
    """
    if obj is None:
        return default
    try:
        values = vars(obj)
    except TypeError:
        return default
    return values.get(name, default)


def _declared_or_explicit_attr(obj: Any, name: str, default=None):
    """Resolve atributo armazenado na instância ou declarado pela classe."""
    instance_value = _explicit_instance_attr(obj, name, _MISSING)
    if instance_value is not _MISSING:
        return instance_value
    if obj is None:
        return default
    class_values = vars(type(obj))
    if name not in class_values:
        return default
    declared = class_values[name]
    descriptor_get = getattr(declared, "__get__", None)
    if callable(descriptor_get):
        return descriptor_get(obj, type(obj))
    return declared


def get_cancel_event(agent_client: Any):
    descriptor = _declared_descriptor(agent_client, "cancel_event")
    if isinstance(descriptor, property):
        return descriptor.__get__(agent_client, type(agent_client))
    return _declared_or_explicit_attr(agent_client, "_cancel_event")


def is_user_cancelled(agent_client: Any) -> bool:
    if agent_client is None:
        return False
    descriptor = _declared_descriptor(agent_client, "user_cancelled")
    if isinstance(descriptor, property):
        return bool(descriptor.__get__(agent_client, type(agent_client)))
    return bool(_declared_or_explicit_attr(agent_client, "_user_cancelled", False))


def mark_user_cancelled(agent_client: Any) -> None:
    if agent_client is None:
        return
    descriptor = _declared_descriptor(agent_client, "user_cancelled")
    setter = getattr(descriptor, "fset", None)
    if callable(setter):
        setter(agent_client, True)
    else:
        setattr(agent_client, "_user_cancelled", True)
    cancel_event = get_cancel_event(agent_client)
    set_event = getattr(cancel_event, "set", None)
    if callable(set_event):
        set_event()


def is_agent_running(agent_client: Any) -> bool:
    if agent_client is None:
        return False
    descriptor = _declared_descriptor(agent_client, "agent_running")
    if isinstance(descriptor, property):
        return bool(descriptor.__get__(agent_client, type(agent_client)))
    return bool(_declared_or_explicit_attr(agent_client, "_agent_running", False))


def get_pause_idle_if(agent_client: Any):
    descriptor = _declared_descriptor(agent_client, "pause_idle_if")
    if isinstance(descriptor, property):
        return descriptor.__get__(agent_client, type(agent_client))
    return _declared_or_explicit_attr(agent_client, "_pause_idle_if")


def share_cancel_event(agent_client: Any, cancel_event: Any) -> None:
    method = getattr(type(agent_client), "share_cancel_event", None)
    if callable(method):
        method(agent_client, cancel_event)
        return
    setattr(agent_client, "_cancel_event", cancel_event)
