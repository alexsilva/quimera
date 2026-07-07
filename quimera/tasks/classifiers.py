"""Classificadores de resultado de execução/review de tasks.

Extraídos de ``AppTaskServices`` para reduzir acoplamento e permitir
testes isolados.
"""
from __future__ import annotations

import re
from typing import Tuple

from ..constants import CMD_TASK
from .planning import normalize_task_description


def classify_task_execution_result(response: str | None) -> Tuple[bool, str]:
    """Classifica se a execução de uma task foi bem-sucedida.

    Returns:
        (sucesso, texto_extraido): sucesso é True quando o agente parece
        ter produzido uma resposta substancial.
    """
    if response is None:
        return False, "sem resposta do agente"
    text = response.strip()
    if not text:
        return False, "resposta vazia do agente"
    lowered = text.lower()
    blocked_markers = (
        "não consigo", "nao consigo", "não posso", "nao posso", "não tenho como", "nao tenho como",
        "não tenho capacidade", "nao tenho capacidade", "não é possível realizar", "nao e possivel realizar",
        "fora do meu escopo", "não está no meu escopo", "nao esta no meu escopo",
        "unable to", "unable to complete", "cannot", "can't", "i'm not able to", "i am not able to",
        "i'm unable to", "i am unable to", "beyond my capabilities", "outside my scope", "outside the scope",
        "impossível", "impossivel", "requer ferramentas", "requires tools",
        "não tenho acesso", "nao tenho acesso", "sem acesso a", "without access to",
        "não tenho permissão", "nao tenho permissao", "preciso de mais informações", "preciso de mais detalhes",
        "need more information", "need more details", "more information is needed",
        "não é minha responsabilidade", "nao e minha responsabilidade", "fora das minhas capacidades",
        "not within my capabilities", "not my responsibility",
    )
    if any(marker in lowered for marker in blocked_markers):
        return False, text
    return True, text


def classify_task_review_result(response: str | None) -> Tuple[bool, str, str]:
    """Classifica o resultado de um review de task.

    Returns:
        (sucesso, verdict, texto): sucesso é True quando o revisor aprovou (ACEITE).
    """
    if response is None:
        return False, "RETENTATIVA", "sem resposta do revisor"

    text = response.strip()
    if not text:
        return False, "RETENTATIVA", "resposta vazia do revisor"

    match = re.search(r"\b(ACEITE|RETENTATIVA|REPLANEJAR|REJEITAR)\b", text.upper())
    if not match:
        return False, "RETENTATIVA", text
    verdict = match.group(1)

    lines = text.split("\n")
    has_justification = any(
        line.strip() and not re.match(r"^\s*(ACEITE|RETENTATIVA|REPLANEJAR|REJEITAR)\s*$", line, re.IGNORECASE)
        for line in lines
    )
    if verdict == "ACEITE" and not has_justification:
        return False, "RETENTATIVA", "ACEITE sem justificativa"

    return verdict == "ACEITE", verdict, text


def parse_task_command(command: str) -> str:
    """Interpreta o conteúdo de um comando ``/task <descrição>``."""
    raw = command[len(CMD_TASK):].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1].strip()
    return normalize_task_description(raw)
