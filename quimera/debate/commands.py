"""Strict command parser for the ``/debate`` surface."""

from __future__ import annotations

import argparse
import shlex
from dataclasses import dataclass

from .models import DebateMode


MAX_DEBATE_TOPIC_CHARS = 8_000


class DebateCommandError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DebateCommandError(message)


@dataclass(frozen=True, slots=True)
class DebateCommand:
    action: str
    topic: str = ""
    mode: DebateMode = DebateMode.VERDICT
    agents: tuple[str, ...] = ()
    rounds: int = 2
    timeout_seconds: float = 300.0
    quorum: int | None = None
    debate_id: str = ""


def parse_debate_command(command: str) -> DebateCommand:
    raw = str(command or "").strip()
    if not raw.startswith("/debate"):
        raise DebateCommandError("comando deve iniciar com /debate")
    try:
        tokens = shlex.split(raw[len("/debate") :].strip())
    except ValueError as exc:
        raise DebateCommandError(str(exc)) from exc
    if not tokens:
        raise DebateCommandError(_usage())

    action = tokens[0].lower()
    if action in {"status", "cancel", "show", "apply", "list"}:
        return _parse_control(action, tokens[1:])

    parser = _Parser(add_help=False, prog="/debate")
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in DebateMode],
        default=DebateMode.VERDICT.value,
    )
    parser.add_argument("--agents", default="")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--quorum", type=int)
    parser.add_argument("topic", nargs="+")
    try:
        namespace = parser.parse_intermixed_args(tokens)
    except AttributeError:
        namespace = parser.parse_args(tokens)
    if not 1 <= namespace.rounds <= 6:
        raise DebateCommandError("--rounds deve estar entre 1 e 6")
    if not 30 <= namespace.timeout <= 1800:
        raise DebateCommandError("--timeout deve estar entre 30 e 1800 segundos")
    if namespace.quorum is not None and namespace.quorum < 2:
        raise DebateCommandError("--quorum deve ser pelo menos 2")
    agents = tuple(
        dict.fromkeys(
            item.strip().lower().lstrip("/")
            for item in namespace.agents.split(",")
            if item.strip()
        )
    )
    if len(agents) > 5:
        raise DebateCommandError("/debate aceita no maximo 5 agentes")
    topic = " ".join(namespace.topic).strip()
    if not topic:
        raise DebateCommandError(_usage())
    if len(topic) > MAX_DEBATE_TOPIC_CHARS:
        raise DebateCommandError(
            f"tema excede o limite de {MAX_DEBATE_TOPIC_CHARS} caracteres"
        )
    return DebateCommand(
        action="start",
        topic=topic,
        mode=DebateMode(namespace.mode),
        agents=agents,
        rounds=namespace.rounds,
        timeout_seconds=namespace.timeout,
        quorum=namespace.quorum,
    )


def _parse_control(action: str, args: list[str]) -> DebateCommand:
    if action == "list":
        if args:
            raise DebateCommandError("Uso: /debate list")
        return DebateCommand(action="list")
    if action in {"status", "cancel"}:
        if len(args) > 1:
            raise DebateCommandError(f"Uso: /debate {action} [id]")
        return DebateCommand(action=action, debate_id=args[0] if args else "")
    if len(args) != 1:
        raise DebateCommandError(f"Uso: /debate {action} <id>")
    return DebateCommand(action=action, debate_id=args[0])


def _usage() -> str:
    return (
        "Uso: /debate [--mode verdict|workflow] [--agents a,b,c] "
        "[--rounds 2] [--timeout 300] [--quorum 2] <tema>"
    )
