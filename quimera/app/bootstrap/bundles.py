"""Dataclasses imutáveis produzidas por cada builder do `AppAssembler`.

Cada bundle agrupa os colaboradores construídos em uma fase de
`AppAssembler.assemble`. Os campos refletem exatamente os objetos hoje
atribuídos em `QuimeraApp.__init__`; nada aqui muda comportamento, apenas
nomeia e organiza o que já existia.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PlatformBundle:
    """Fundações independentes de UI: workspace, config, storage, policy."""

    lock: Any
    output_lock: Any
    counter_lock: Any
    selected_agents: list
    agent_pool: Any
    threads: int
    toolbar: Any
    auto_approve_mutations: bool
    profile_registry: Any
    workspace: Any
    config: Any
    workspace_policy_name: str
    workspace_policy: Any
    active_theme: str | None
    storage: Any
    session_started_at: float
    bug_store: Any
    bug_detector: Any
    agent_bug_detector: Any
    bug_correlator: Any
    session_id: str
    render_log_path: Path
    render_ansi_path: Path
    metrics_file: Path | None
    app_log_path: Path | None


@dataclass(frozen=True)
class UiBundle:
    """Renderer e canais de entrada/saída de baixo nível."""

    renderer: Any
    agent_run_sink: Any
    event_sink: Any
    user_name: str
    visibility: Any
    session_metrics: Any
    history_file: Path
    input_gate: Any
    input_broker: Any


@dataclass(frozen=True)
class SessionBundle:
    """Estado de sessão único e serviços que dependem dele."""

    context_manager: Any
    configured_history_window: int | None
    configured_auto_summarize_threshold: int
    history: list
    history_restored: bool
    session_runtime_state: Any
    session_state_mgr: Any
    shared_state: dict
    turn_stamps: dict
    shared_state_lock: Any
    history_lock: Any
    display_service: Any
    profile_resolver: Any
    system_layer: Any
    input_services: Any


@dataclass(frozen=True)
class RuntimeBundle:
    """Cliente de agente, protocolo e estado de runtime da rodada de chat."""

    workspace_tmp_root: Any
    idle_timeout_seconds: int
    process_supervisor: Any
    agent_client: Any
    task_executor_factory: Any
    session_summarizer: Any
    summary_loaded: bool
    session_state: Any
    behavior_metrics: Any
    debug_prompt_metrics: bool
    chat_state: Any
    protocol: Any
    runtime_state: Any
    deferred_system_messages: list
    max_deferred_system_messages: int
    turn_manager: Any
    is_new_session: bool
    tasks_db_path: str
    current_job_id: int
    previous_current_job_id_env: str | None
    prompt_builder: Any
    auto_summarize_threshold: int


@dataclass(frozen=True)
class TaskBundle:
    """Serviços de execução e despacho de tarefas/agentes."""

    task_services: Any
    session_services: Any
    dispatch_services: Any
    tool_executor: Any


@dataclass(frozen=True)
class ChatBundle:
    """Orquestração de rodadas de chat, toolbar e serviços auxiliares."""

    chat_round_orchestrator: Any
    ui_event_handler: Any
    toolbar_coordinator: Any
    chat_lifecycle: Any
    bug_services: Any
    failure_tracker: Any
    command_router: Any


@dataclass(frozen=True)
class AppBundles:
    """Agregado de todos os bundles produzidos por `AppAssembler.assemble`."""

    platform: PlatformBundle
    ui: UiBundle
    session: SessionBundle
    runtime: RuntimeBundle
    tasks: TaskBundle
    chat: ChatBundle
