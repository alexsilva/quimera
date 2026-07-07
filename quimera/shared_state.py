"""Contrato e helpers do shared_state."""

from __future__ import annotations

import json

# Campos que agentes podem escrever via a tool MCP update_shared_state.
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

VOLATILE_AGENT_STATE_KEYS = {
    "goal",
    "goal_canonical",
    "current_step",
    "next_step",
}

# Campos de runtime escritos apenas pelo sistema.
SYSTEM_STATE_KEYS = {
    "task_overview",
    "completed_task_results",
    "spy_last_turn_detail",
    "working_dir",
    "workspace_root",
    "agent_todos",
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

# Limites de tamanho para valores escritos por agentes via update_shared_state.
# Evitam que um payload malicioso/descontrolado infle o shared_state_json
# injetado no prompt (custo de tokens e superfície de prompt injection).
MAX_AGENT_STRING_LENGTH = 4000
MAX_AGENT_LIST_ITEM_LENGTH = 2000
MAX_AGENT_UPDATE_KEYS = len(AGENT_STATE_KEYS)

PROMPT_HIDDEN_KEYS = AGENT_STATE_KEYS

TASK_REFERENCE_KEYS = AGENT_STATE_KEYS | {"task_overview"}
PROMPT_REFERENCE_KEYS = {"task_overview", "working_dir", "workspace_root", "agent_todos"}


def normalize_state_key(key) -> str:
    """Normaliza uma chave externa para o formato interno."""
    return str(key).strip().lower().replace(" ", "_")


def is_agent_state_key(key: str) -> bool:
    """Indica se a chave pode ser escrita por agentes."""
    return normalize_state_key(key) in AGENT_STATE_KEYS


def validate_agent_state_value(key: str, value) -> bool:
    """Valida tipos e tamanhos do contrato aceito de agentes.

    Além do tipo, limita o tamanho de strings e itens de lista para impedir
    que um agente infle o shared_state_json injetado no prompt (custo de
    tokens e superfície de prompt injection).
    """
    normalized = normalize_state_key(key)
    if normalized in LIST_STATE_KEYS:
        if value is None or value == "":
            return True
        if not isinstance(value, list):
            return False
        return all(
            isinstance(item, str) and len(item) <= MAX_AGENT_LIST_ITEM_LENGTH
            for item in value
        )
    if normalized in STRING_STATE_KEYS:
        if value is None or value == "":
            return True
        return isinstance(value, str) and len(value) <= MAX_AGENT_STRING_LENGTH
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


# --- TTL por turno para agent keys ---

# Número máximo de turnos que uma agent key sobrevive sem reafirmação.
STATE_KEY_MAX_AGE_TURNS = 10

def stamp_state_keys(turn_stamps: dict, keys: set[str], current_turn: int) -> None:
    """Registra o turno atual para cada key atualizada.

    ``turn_stamps`` é um dicionário externo mantido separadamente do
    ``shared_state`` para não poluir o estado visível a agentes/testes.
    """
    for key in keys:
        if key in AGENT_STATE_KEYS:
            turn_stamps[key] = current_turn


def bootstrap_state_key_stamps(shared_state: dict, turn_stamps: dict, current_turn: int = 0) -> None:
    """Inicializa stamps para agent keys já presentes no estado restaurado."""
    if not isinstance(shared_state, dict):
        return
    for key in AGENT_STATE_KEYS:
        if key in shared_state:
            turn_stamps.setdefault(key, current_turn)


def expire_stale_keys(shared_state: dict, turn_stamps: dict, current_turn: int, max_age: int = STATE_KEY_MAX_AGE_TURNS) -> list[str]:
    """Remove agent keys não reafirmadas nos últimos ``max_age`` turnos.

    ``turn_stamps`` é um dicionário externo de {key: last_turn}.
    Retorna lista de keys expiradas.
    """
    expired = []
    for key in list(turn_stamps):
        if key not in AGENT_STATE_KEYS:
            turn_stamps.pop(key, None)
            continue
        age = current_turn - turn_stamps[key]
        if age > max_age:
            shared_state.pop(key, None)
            turn_stamps.pop(key, None)
            expired.append(key)
    return expired


def clear_agent_state_for_session_start(shared_state: dict, *, history_restored: bool) -> list[str]:
    """Remove estado ativo que não deve iniciar uma nova execução interativa."""
    if not isinstance(shared_state, dict):
        return []
    keys = VOLATILE_AGENT_STATE_KEYS if history_restored else AGENT_STATE_KEYS
    removed = []
    for key in keys:
        if key in shared_state:
            shared_state.pop(key, None)
            removed.append(key)
    if not history_restored and "_current_turn" in shared_state:
        shared_state.pop("_current_turn", None)
        removed.append("_current_turn")
    return removed
