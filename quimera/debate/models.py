"""Domain models and strict protocol parsing for coordinated debates."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable


MAX_DEBATE_RESPONSE_CHARS = 128_000
MAX_DEBATE_TEXT_CHARS = 20_000
MAX_DEBATE_LIST_ITEMS = 32
MAX_DEBATE_WORK_ITEMS = 50
_WORK_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TASK_TYPES = frozenset(
    {
        "architecture",
        "bug_investigation",
        "code_edit",
        "code_review",
        "documentation",
        "general",
        "test_execution",
    }
)


class DebateProtocolError(ValueError):
    """Raised when an agent response violates the debate protocol."""


class DebateMode(str, Enum):
    VERDICT = "verdict"
    WORKFLOW = "workflow"


class DebateStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    SYNTHESIZING = "synthesizing"
    CONVERGED = "converged"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_DEBATE_STATUSES = frozenset(
    {
        DebateStatus.CONVERGED,
        DebateStatus.EXHAUSTED,
        DebateStatus.CANCELLED,
        DebateStatus.FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class DebateLimits:
    max_rounds: int = 2
    timeout_seconds: float = 900.0
    quorum: int = 2


@dataclass(frozen=True, slots=True)
class WorkItem:
    id: str
    title: str
    description: str
    task_type: str = "general"
    assigned_to: str | None = None
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    priority: str = "medium"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DebateContribution:
    debate_id: str
    round_index: int
    agent: str
    position: str
    arguments: tuple[str, ...] = ()
    objections: tuple[str, ...] = ()
    proposal: str = ""
    confidence: float = 0.0
    vote: str = "abstain"
    critical_objection: bool = False
    work_items: tuple[WorkItem, ...] = ()
    raw_response: str = field(default="", repr=False)

    def as_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_raw:
            data.pop("raw_response", None)
        return data

    def render(self) -> str:
        lines = [self.position.strip()]
        if self.arguments:
            lines.extend(
                ["", "**Argumentos**", *[f"- {item}" for item in self.arguments]]
            )
        if self.objections:
            lines.extend(
                ["", "**Objecoes**", *[f"- {item}" for item in self.objections]]
            )
        if self.proposal:
            lines.extend(["", "**Proposta**", self.proposal.strip()])
        if self.work_items:
            lines.extend(["", "**Divisao de trabalho**"])
            for item in self.work_items:
                owner = f" -> {item.assigned_to}" if item.assigned_to else ""
                lines.append(f"- `{item.id}` {item.title}{owner}")
        vote = {
            "support": "apoio",
            "oppose": "oposicao",
            "abstain": "abstencao",
            "propose": "proposta inicial",
        }.get(self.vote, self.vote)
        lines.extend(["", f"Voto: **{vote}** | confianca: **{self.confidence:.0%}**"])
        return "\n".join(line for line in lines if line is not None).strip()


@dataclass(frozen=True, slots=True)
class DebateSynthesis:
    debate_id: str
    round_index: int
    moderator: str
    summary: str
    verdict: str
    agreements: tuple[str, ...] = ()
    disagreements: tuple[str, ...] = ()
    critical_objections: tuple[str, ...] = ()
    confidence: float = 0.0
    consensus_reached: bool = False
    work_items: tuple[WorkItem, ...] = ()
    raw_response: str = field(default="", repr=False)

    def as_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_raw:
            data.pop("raw_response", None)
        return data

    def render(self, *, final: bool = False, status: DebateStatus | None = None) -> str:
        lines = [self.verdict.strip() or self.summary.strip()]
        if self.work_items:
            lines.extend(["", "**Plano coordenado**"])
            for item in self.work_items:
                owner = item.assigned_to or "nao atribuido"
                deps = (
                    f" | depende de: {', '.join(item.dependencies)}"
                    if item.dependencies
                    else ""
                )
                lines.append(f"- `{item.id}` **{item.title}** -> {owner}{deps}")
                if item.acceptance_criteria:
                    lines.extend(
                        f"  - aceite: {criterion}"
                        for criterion in item.acceptance_criteria
                    )
        if self.agreements:
            lines.extend(
                ["", "**Acordos**", *[f"- {item}" for item in self.agreements]]
            )
        dissent = (*self.disagreements, *self.critical_objections)
        if dissent:
            lines.extend(
                ["", "**Dissensos**", *[f"- {item}" for item in dict.fromkeys(dissent)]]
            )
        if final:
            result = "consenso" if status == DebateStatus.CONVERGED else "sem consenso"
            lines.extend(
                ["", f"Resultado: **{result}** | confianca: **{self.confidence:.0%}**"]
            )
        return "\n".join(lines).strip()


@dataclass(frozen=True, slots=True)
class DebateSession:
    id: str
    session_id: str
    topic: str
    mode: DebateMode
    status: DebateStatus
    participants: tuple[str, ...]
    moderator: str
    limits: DebateLimits
    context: str = ""
    current_round: int = 0
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    result: DebateSynthesis | None = None
    error: str = ""
    applied_at: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_DEBATE_STATUSES


def extract_json_object(response: Any) -> dict[str, Any]:
    """Extract the first JSON object without accepting trailing prose as data."""
    if isinstance(response, dict):
        return dict(response)
    text = str(response or "").strip()
    if not text:
        raise DebateProtocolError("resposta vazia")
    if len(text) > MAX_DEBATE_RESPONSE_CHARS:
        raise DebateProtocolError("resposta excede o limite do protocolo")
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise DebateProtocolError("resposta nao contem objeto JSON valido")


def contribution_from_response(
    response: Any,
    *,
    debate_id: str,
    round_index: int,
    agent: str,
) -> DebateContribution:
    payload = extract_json_object(response)
    position = _required_text(payload, "position")
    proposal = _text(payload.get("proposal"), field_name="proposal")
    work_items = _work_items(payload.get("work_items", ()))
    vote = _text(payload.get("vote") or "abstain", field_name="vote").lower()
    if vote not in {"support", "oppose", "abstain", "propose"}:
        raise DebateProtocolError(f"vote invalido: {vote}")
    return DebateContribution(
        debate_id=debate_id,
        round_index=round_index,
        agent=agent,
        position=position,
        arguments=_text_items(payload.get("arguments"), field_name="arguments"),
        objections=_text_items(payload.get("objections"), field_name="objections"),
        proposal=proposal,
        confidence=_confidence(payload.get("confidence"), required=True),
        vote=vote,
        critical_objection=_boolean(payload, "critical_objection", default=False),
        work_items=work_items,
        raw_response=str(response or ""),
    )


def synthesis_from_response(
    response: Any,
    *,
    debate_id: str,
    round_index: int,
    moderator: str,
) -> DebateSynthesis:
    payload = extract_json_object(response)
    work_items = _work_items(payload.get("work_items", ()))
    validate_work_items(work_items)
    return DebateSynthesis(
        debate_id=debate_id,
        round_index=round_index,
        moderator=moderator,
        summary=_required_text(payload, "summary"),
        verdict=_required_text(payload, "verdict"),
        agreements=_text_items(payload.get("agreements"), field_name="agreements"),
        disagreements=_text_items(
            payload.get("disagreements"), field_name="disagreements"
        ),
        critical_objections=_text_items(
            payload.get("critical_objections"),
            field_name="critical_objections",
        ),
        confidence=_confidence(payload.get("confidence"), required=True),
        consensus_reached=_boolean(payload, "consensus_reached"),
        work_items=work_items,
        raw_response=str(response or ""),
    )


def validate_work_items(items: Iterable[WorkItem]) -> None:
    """Validate IDs and acyclicity of a workflow plan."""
    work_items = tuple(items)
    by_id = {item.id: item for item in work_items}
    if len(by_id) != len(work_items):
        raise DebateProtocolError("work_items contem IDs duplicados")
    for item in work_items:
        unknown = set(item.dependencies) - set(by_id)
        if unknown:
            raise DebateProtocolError(
                f"work item {item.id} depende de IDs desconhecidos: {', '.join(sorted(unknown))}"
            )
        if item.id in item.dependencies:
            raise DebateProtocolError(f"work item {item.id} depende de si mesmo")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            raise DebateProtocolError("workflow contem ciclo de dependencias")
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in by_id[item_id].dependencies:
            visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in by_id:
        visit(item_id)


def synthesis_from_dict(payload: dict[str, Any] | None) -> DebateSynthesis | None:
    if not payload:
        return None
    return DebateSynthesis(
        debate_id=_text(payload.get("debate_id"), field_name="debate_id"),
        round_index=int(payload.get("round_index") or 0),
        moderator=_text(payload.get("moderator"), field_name="moderator"),
        summary=_text(payload.get("summary"), field_name="summary"),
        verdict=_text(payload.get("verdict"), field_name="verdict"),
        agreements=_text_items(payload.get("agreements"), field_name="agreements"),
        disagreements=_text_items(
            payload.get("disagreements"), field_name="disagreements"
        ),
        critical_objections=_text_items(
            payload.get("critical_objections"),
            field_name="critical_objections",
        ),
        confidence=_confidence(payload.get("confidence")),
        consensus_reached=_stored_bool(payload.get("consensus_reached", False)),
        work_items=_work_items(payload.get("work_items", ())),
        raw_response=_text(
            payload.get("raw_response"),
            field_name="raw_response",
            max_chars=MAX_DEBATE_RESPONSE_CHARS,
        ),
    )


def _work_items(value: Any) -> tuple[WorkItem, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        raise DebateProtocolError("work_items deve ser uma lista")
    if len(value) > MAX_DEBATE_WORK_ITEMS:
        raise DebateProtocolError(
            f"work_items excede o limite de {MAX_DEBATE_WORK_ITEMS} itens"
        )
    items: list[WorkItem] = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise DebateProtocolError("cada work_item deve ser um objeto")
        item_id = _text(
            raw.get("id") or f"T{index}", field_name="work_item.id", max_chars=64
        )
        if not _WORK_ITEM_ID_RE.fullmatch(item_id):
            raise DebateProtocolError(f"work_item possui id invalido: {item_id}")
        title = _required_text(raw, "title", max_chars=500)
        description = _text(
            raw.get("description") or title,
            field_name="work_item.description",
            max_chars=8_000,
        )
        priority = _text(
            raw.get("priority") or "medium",
            field_name="work_item.priority",
            max_chars=20,
        ).lower()
        if priority not in {"low", "medium", "high", "critical"}:
            raise DebateProtocolError(
                f"prioridade invalida no work_item {item_id}: {priority}"
            )
        task_type = _text(
            raw.get("task_type") or "general",
            field_name="work_item.task_type",
            max_chars=40,
        ).lower()
        if task_type not in _TASK_TYPES:
            raise DebateProtocolError(
                f"task_type invalido no work_item {item_id}: {task_type}"
            )
        items.append(
            WorkItem(
                id=item_id,
                title=title,
                description=description,
                task_type=task_type,
                assigned_to=_text(
                    raw.get("assigned_to"),
                    field_name="work_item.assigned_to",
                    max_chars=200,
                )
                or None,
                dependencies=_text_items(
                    raw.get("dependencies"),
                    field_name="work_item.dependencies",
                ),
                acceptance_criteria=_text_items(
                    raw.get("acceptance_criteria"),
                    field_name="work_item.acceptance_criteria",
                ),
                priority=priority,
            )
        )
    return tuple(items)


def _required_text(
    payload: dict[str, Any],
    key: str,
    *,
    max_chars: int = MAX_DEBATE_TEXT_CHARS,
) -> str:
    value = _text(payload.get(key), field_name=key, max_chars=max_chars)
    if not value:
        raise DebateProtocolError(f"campo obrigatorio ausente: {key}")
    return value


def _text(
    value: Any,
    *,
    field_name: str = "campo",
    max_chars: int = MAX_DEBATE_TEXT_CHARS,
) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DebateProtocolError(f"{field_name} deve ser texto")
    text = value.strip()
    if len(text) > max_chars:
        raise DebateProtocolError(
            f"{field_name} excede o limite de {max_chars} caracteres"
        )
    return text


def _text_items(
    value: Any,
    *,
    field_name: str = "lista",
) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        raise DebateProtocolError(f"{field_name} possui tipo invalido")
    if len(value) > MAX_DEBATE_LIST_ITEMS:
        raise DebateProtocolError(
            f"{field_name} excede o limite de {MAX_DEBATE_LIST_ITEMS} itens"
        )
    return tuple(
        text
        for item in value
        if (text := _text(item, field_name=f"{field_name}[]", max_chars=4_000))
    )


def _confidence(value: Any, *, required: bool = False) -> float:
    if value is None and not required:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DebateProtocolError("confidence deve ser numero entre 0 e 1")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise DebateProtocolError("confidence deve estar entre 0 e 1")
    return number


def _boolean(payload: dict[str, Any], key: str, *, default: bool | None = None) -> bool:
    if key not in payload:
        if default is not None:
            return default
        raise DebateProtocolError(f"campo obrigatorio ausente: {key}")
    value = payload[key]
    if not isinstance(value, bool):
        raise DebateProtocolError(f"{key} deve ser booleano")
    return value


def _stored_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise DebateProtocolError("valor booleano persistido invalido")
    return value
