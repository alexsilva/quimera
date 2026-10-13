"""Handoff estruturado entre tarefas: contrato, parsing e verificação determinística.

O agente executor encerra sua resposta com um bloco JSON ``handoff``. O runtime
não toma a afirmação textual como prova: o que dá para conferir (arquivos
declarados existirem no workspace, verificações reportadas como falhas) é
verificado aqui, sem nova chamada de IA.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .models import WorkflowTaskSpec

MAX_SUMMARY_CHARS = 2_000
MAX_LIST_ITEMS = 20
MAX_ITEM_CHARS = 300

_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_STATUSES = frozenset({"success", "partial", "failed"})

HANDOFF_INSTRUCTIONS = (
    "Ao terminar, encerre a resposta com UM bloco ```json com este formato exato "
    "(listas podem ser vazias, mas nao invente conteudo):\n"
    '{"handoff": {"status": "success|partial|failed", "summary": "resumo objetivo", '
    '"files_changed": ["caminho/relativo"], '
    '"checks": [{"command": "o que foi executado", "outcome": "passed|failed", "detail": "saida relevante"}], '
    '"evidence_refs": ["arquivo, log ou artefato que comprova"], '
    '"limitations": ["pendencias e limites"], "failure_reason": null}}'
)


@dataclass(frozen=True, slots=True)
class Check:
    """Verificação (teste, comando, inspeção) reportada pelo executor."""

    command: str
    outcome: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Handoff:
    """Resultado transferível de uma tentativa de execução."""

    task_id: int
    attempt: int
    agent: str
    status: str
    summary: str
    files_changed: tuple[str, ...] = ()
    checks: tuple[Check, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    failure_reason: str | None = None
    structured: bool = True
    truncated: bool = False
    unverified_files: tuple[str, ...] = field(default=())

    def to_json(self) -> str:
        """Serialização persistida em ``tasks.result`` (resumo primeiro)."""
        data = asdict(self)
        ordered = {"summary": data.pop("summary"), **data}
        return json.dumps(ordered, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str | None) -> Handoff | None:
        """Reconstrói um handoff persistido; ``None`` se o texto não for um handoff."""
        try:
            data = json.loads(text or "")
            return cls(
                task_id=int(data["task_id"]),
                attempt=int(data["attempt"]),
                agent=str(data["agent"]),
                status=str(data["status"]),
                summary=str(data["summary"]),
                files_changed=tuple(data.get("files_changed", ())),
                checks=tuple(Check(**check) for check in data.get("checks", ())),
                evidence_refs=tuple(data.get("evidence_refs", ())),
                limitations=tuple(data.get("limitations", ())),
                failure_reason=data.get("failure_reason"),
                structured=bool(data.get("structured", True)),
                truncated=bool(data.get("truncated", False)),
                unverified_files=tuple(data.get("unverified_files", ())),
            )
        except (TypeError, ValueError, KeyError):
            return None


def parse_handoff(text: str, *, task_id: int, attempt: int, agent: str) -> Handoff:
    """Extrai o handoff da resposta; sem bloco válido devolve um handoff não estruturado."""
    payload = _extract_block(text)
    if payload is None:
        return Handoff(
            task_id=task_id,
            attempt=attempt,
            agent=agent,
            status="success",
            summary=_clip(text.strip(), MAX_SUMMARY_CHARS),
            limitations=("handoff nao estruturado: resultado nao verificavel",),
            structured=False,
            truncated=len(text.strip()) > MAX_SUMMARY_CHARS,
        )
    status = str(payload.get("status") or "").strip().lower()
    if status not in _STATUSES:
        status = "failed"
        payload = {**payload, "failure_reason": payload.get("failure_reason") or "status invalido"}
    files, cut_files = _items(payload.get("files_changed"))
    refs, cut_refs = _items(payload.get("evidence_refs"))
    limits, cut_limits = _items(payload.get("limitations"))
    checks = tuple(_checks(payload.get("checks")))
    summary = str(payload.get("summary") or "").strip()
    reason = payload.get("failure_reason")
    return Handoff(
        task_id=task_id,
        attempt=attempt,
        agent=agent,
        status=status,
        summary=_clip(summary, MAX_SUMMARY_CHARS),
        files_changed=files,
        checks=checks,
        evidence_refs=refs,
        limitations=limits,
        failure_reason=str(reason) if reason else None,
        truncated=cut_files or cut_refs or cut_limits or len(summary) > MAX_SUMMARY_CHARS,
    )


def verify_files(handoff: Handoff, workspace_root: Path) -> Handoff:
    """Marca como não verificados os arquivos declarados que não existem no workspace."""
    root = workspace_root.resolve()
    missing = []
    for raw in handoff.files_changed:
        candidate = (root / raw).resolve()
        if root not in candidate.parents and candidate != root:
            missing.append(raw)
        elif not candidate.exists():
            missing.append(raw)
    if not missing:
        return handoff
    return replace(handoff, unverified_files=tuple(missing))


def evaluate(handoff: Handoff, spec: WorkflowTaskSpec) -> tuple[bool, str]:
    """Decide, sem IA, se a evidência basta para submeter a task à revisão/conclusão."""
    if handoff.status == "failed":
        return False, handoff.failure_reason or "executor reportou falha"
    if handoff.unverified_files:
        return False, "arquivos declarados inexistentes: " + ", ".join(handoff.unverified_files)
    failed = [check.command for check in handoff.checks if check.outcome.lower() == "failed"]
    if failed:
        return False, "verificacoes falharam: " + "; ".join(failed)
    if spec.evidence_expected:
        if not handoff.structured:
            return False, "evidencia esperada, mas o handoff nao e estruturado"
        if not handoff.checks and not handoff.evidence_refs:
            return False, "evidencia esperada ausente (sem verificacoes nem referencias)"
    if handoff.status == "partial":
        return False, "executor reportou resultado parcial: " + (
            "; ".join(handoff.limitations) or "sem detalhes"
        )
    return True, handoff.summary


def _extract_block(text: str) -> dict[str, Any] | None:
    for match in reversed(list(_FENCED_JSON.finditer(text or ""))):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        inner = data.get("handoff") if isinstance(data, dict) else None
        if isinstance(inner, dict):
            return inner
    return None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _items(value: Any) -> tuple[tuple[str, ...], bool]:
    if not isinstance(value, list):
        return (), False
    items = [_clip(str(item).strip(), MAX_ITEM_CHARS) for item in value if str(item).strip()]
    return tuple(items[:MAX_LIST_ITEMS]), len(items) > MAX_LIST_ITEMS


def _checks(value: Any) -> list[Check]:
    if not isinstance(value, list):
        return []
    checks = []
    for raw in value[:MAX_LIST_ITEMS]:
        if isinstance(raw, dict) and raw.get("command"):
            checks.append(
                Check(
                    command=_clip(str(raw["command"]), MAX_ITEM_CHARS),
                    outcome=str(raw.get("outcome") or "unknown").lower(),
                    detail=_clip(str(raw.get("detail") or ""), MAX_ITEM_CHARS),
                )
            )
    return checks
