"""Componentes de `quimera.constants`.

Fachada de re-export temporária — o código de domínio foi movido para:
  - ``quimera.domain.task_states`` (Visibility, TaskStatus, TaskType, can_transition)
  - ``quimera.runtime.tool_schema_defs`` (TOOL_SCHEMA, build_tools_prompt)
  - ``quimera.ui.commands`` (CMD_*, MSG_*, build_help, build_agents_help, etc.)
"""
from __future__ import annotations

import os

# --- Domínio: re-exports de quimera.domain.task_states ---
from quimera.domain.task_states import (  # noqa: F401
    Visibility,
    TaskStatus,
    VALID_TRANSITIONS,
    can_transition,
    TaskType,
)

# --- Runtime: re-exports de quimera.runtime.tool_schema_defs ---
from quimera.runtime.tool_schema_defs import (  # noqa: F401
    TOOL_SCHEMA,
    build_tools_prompt,
)

# --- UI / Comandos: re-exports de quimera.ui.commands ---
from quimera.ui.commands import (  # noqa: F401
    DEFAULT_FIRST_AGENT,
    INPUT_PROMPT,
    EXTEND_MARKER,
    CMD_EXIT,
    CMD_CLEAR,
    CMD_PROMPT,
    CMD_HELP,
    CMD_AGENTS,
    CMD_CONNECT,
    CMD_DISCONNECT,
    CMD_RELOAD,
    CMD_CONTEXT,
    CMD_CONTEXT_EDIT,
    CMD_CONTEXT_BRANCH,
    CMD_EDIT,
    CMD_FILE_PREFIX,
    CMD_TASK,
    CMD_BUGS,
    CMD_RESET,
    CMD_APPROVE,
    CMD_APPROVE_ALL,
    CMD_POLICY,
    CMD_CONFIG,
    CMD_ALIASES,
    USER_ROLE,
    MSG_CHAT_STARTED,
    MSG_SESSION_LOG,
    MSG_SESSION_STATUS,
    MSG_MIGRATION,
    MSG_MEMORY_SAVING,
    MSG_MEMORY_FAILED,
    MSG_SHUTDOWN,
    MSG_DOUBLE_PREFIX,
    MSG_EMPTY_INPUT,
    build_help,
    build_agents_help,
)

# --- Constantes que permanecem aqui (sem domínio claro de movimentação) ---
MAX_STDERR_LINES = 5
_env_limit = os.getenv("QUIMERA_MAX_STDERR_LINES")
if _env_limit is not None:
    try:
        MAX_STDERR_LINES = int(_env_limit)
    except Exception:
        pass

# Shared state keys that should be trimmed when building prompts
_SHARED_STATE_TRIM_KEYS = [
    "goal_canonical", "current_step", "acceptance_criteria", "allowed_scope",
    "non_goals", "out_of_scope_notes", "next_step", "task_overview",
]
