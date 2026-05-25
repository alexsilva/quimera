"""Bootstrap de infraestrutura base do QuimeraApp."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .event_sink import EventSink
from .session_paths import (
    resolve_workspace_metrics_path,
    resolve_workspace_render_ansi_path,
    resolve_workspace_render_log_path,
)
from ..bugs import AgentRuntimeBugDetector, BugCorrelator, BugStore, RenderBugDetector
from ..config import ConfigManager
from ..env_config import EnvConfig
from ..storage import SessionStorage
from ..ui import RenderAuditLogger, TerminalRenderer
from ..workspace import Workspace


@dataclass(frozen=True)
class AppInfrastructure:
    workspace: Any
    config: Any
    storage: Any
    bug_store: Any
    bug_detector: Any
    agent_bug_detector: Any
    bug_correlator: Any
    render_log_path: Path | None
    render_ansi_path: Path | None
    metrics_file: Path | None
    renderer: Any
    event_sink: Any


class AppBootstrapper:
    """Cria infraestrutura básica sem acoplar o restante do core."""

    def __init__(
        self,
        cwd: Path,
        *,
        debug: bool,
        theme: str | None,
        workspace: Any = None,
        workspace_cls: Any = Workspace,
        env_config_cls: Any = EnvConfig,
        config_manager_cls: Any = ConfigManager,
        session_storage_cls: Any = SessionStorage,
        bug_store_cls: Any = BugStore,
        render_bug_detector_cls: Any = RenderBugDetector,
        agent_runtime_bug_detector_cls: Any = AgentRuntimeBugDetector,
        bug_correlator_cls: Any = BugCorrelator,
        render_audit_logger_cls: Any = RenderAuditLogger,
        terminal_renderer_cls: Any = TerminalRenderer,
        event_sink_cls: Any = EventSink,
    ) -> None:
        self.cwd = cwd
        self.debug = debug
        self.theme = theme
        self.workspace = workspace
        self.workspace_cls = workspace_cls
        self.env_config_cls = env_config_cls
        self.config_manager_cls = config_manager_cls
        self.session_storage_cls = session_storage_cls
        self.bug_store_cls = bug_store_cls
        self.render_bug_detector_cls = render_bug_detector_cls
        self.agent_runtime_bug_detector_cls = agent_runtime_bug_detector_cls
        self.bug_correlator_cls = bug_correlator_cls
        self.render_audit_logger_cls = render_audit_logger_cls
        self.terminal_renderer_cls = terminal_renderer_cls
        self.event_sink_cls = event_sink_cls

    def build_infrastructure(self, *, get_plugin_style: Callable[[str], dict | None]) -> AppInfrastructure:
        workspace = self.workspace if self.workspace is not None else self.workspace_cls(self.cwd)
        self.env_config_cls(workspace.env_file).apply_to_environ()

        config = self.config_manager_cls(workspace.config_file)
        active_theme = self.theme if self.theme is not None else config.theme
        storage = self.session_storage_cls(workspace.logs_dir)

        bug_store = self.bug_store_cls(workspace.tmp.root / "data" / "logs")
        bug_detector = self.render_bug_detector_cls(repeat_threshold=2)
        agent_bug_detector = self.agent_runtime_bug_detector_cls()
        bug_correlator = self.bug_correlator_cls(window_seconds=60.0)

        session_id = storage.session_id
        render_log_path = resolve_workspace_render_log_path(workspace, session_id)
        render_ansi_path = resolve_workspace_render_ansi_path(workspace, session_id)
        metrics_file = resolve_workspace_metrics_path(workspace, session_id) if self.debug else None

        render_audit_logger = (
            self.render_audit_logger_cls(render_log_path, render_ansi_path) if self.debug else None
        )
        renderer = self.terminal_renderer_cls(
            theme=active_theme,
            get_plugin_style=get_plugin_style,
            density=config.density,
            audit_logger=render_audit_logger,
        )
        event_sink = self.event_sink_cls()

        return AppInfrastructure(
            workspace=workspace,
            config=config,
            storage=storage,
            bug_store=bug_store,
            bug_detector=bug_detector,
            agent_bug_detector=agent_bug_detector,
            bug_correlator=bug_correlator,
            render_log_path=render_log_path,
            render_ansi_path=render_ansi_path,
            metrics_file=metrics_file,
            renderer=renderer,
            event_sink=event_sink,
        )
