"""Contrato e helpers do shared_state."""

from __future__ import annotations

import json

# Campos que agentes podem escrever via [STATE_UPDATE].
AGENT_STATE_KEYS = {
    "goal",
    "goal_canonical",
    "decisions",
    "current_step",
    "acceptance_criteria",
    "allowed_scope",
    "non_goals",
    "out_of_scope_notes",
    "evidence",
    "next_step",
}

# Campos de runtime escritos apenas pelo sistema.
SYSTEM_STATE_KEYS = {
    "task_overview",
    "completed_task_results",
    "spy_last_turn_detail",
    "working_dir",
    "workspace_root",
}

LIST_STATE_KEYS = {
    "decisions",
    "acceptance_criteria",
    "allowed_scope",
    "non_goals",
    "out_of_scope_notes",
    "evidence",
}

STRING_STATE_KEYS = {
    "goal",
    "goal_canonical",
    "current_step",
    "next_step",
}

PROMPT_HIDDEN_KEYS = AGENT_STATE_KEYS

TASK_REFERENCE_KEYS = AGENT_STATE_KEYS | {"task_overview"}
PROMPT_REFERENCE_KEYS = {"task_overview", "working_dir", "workspace_root"}


def normalize_state_key(key) -> str:
    """Normaliza uma chave externa para o formato interno."""
    return str(key).strip().lower().replace(" ", "_")


def is_agent_state_key(key: str) -> bool:
    """Indica se a chave pode ser escrita por agentes."""
    return normalize_state_key(key) in AGENT_STATE_KEYS


def validate_agent_state_value(key: str, value) -> bool:
    """Valida tipos básicos do contrato aceito de agentes."""
    normalized = normalize_state_key(key)
    if normalized in LIST_STATE_KEYS:
        return value is None or value == "" or isinstance(value, list)
    if normalized in STRING_STATE_KEYS:
        return value is None or value == "" or isinstance(value, str)
    return True


def trim_state(state, *, allowed_keys, decisions_tail=5) -> dict:
    """Mantém apenas o subconjunto permitido do estado."""
    trimmed = {}
    for key in allowed_keys:
        if key not in state:
            continue
        value = state[key]
        if key == "decisions" and isinstance(value, list):
            trimmed[key] = value[-decisions_tail:]
            continue
        trimmed[key] = value
    return trimmed


def build_prompt_state_payload(shared_state) -> tuple[str, str]:
    """Serializa o estado não operacional exposto no prompt principal."""
    shared = shared_state or {}
    prompt_state = trim_state(shared, allowed_keys=PROMPT_REFERENCE_KEYS)
    shared_state_json = ""
    if prompt_state:
        shared_state_json = json.dumps(prompt_state, ensure_ascii=False, indent=2)
    completed_task_results = shared.get("completed_task_results", "") or ""
    return shared_state_json, completed_task_results


def build_task_reference_payload(shared_state) -> dict:
    """Retorna o subconjunto do estado usado como referência em tasks."""
    shared = shared_state or {}
    return trim_state(shared, allowed_keys=TASK_REFERENCE_KEYS)
