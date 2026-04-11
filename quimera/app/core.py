import hashlib
import json
import logging
import os
import queue
import random
import re
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

try:
    import readline
except ImportError:
    readline = None

from . import inputs as app_input
from . import task as app_tasks
from .system_layer import AppSystemLayer
from .. import plugins
from ..runtime.executor import ToolExecutor
from ..runtime.parser import strip_tool_block
from ..runtime import ToolRuntimeConfig, ConsoleApprovalHandler, create_executor
from ..runtime import tasks as runtime_tasks
from ..ui import TerminalRenderer
from ..context import ContextManager
from ..storage import SessionStorage
from ..agents import AgentClient
from ..session_summary import SessionSummarizer, build_chain_summarizer
from ..prompt import PromptBuilder
from ..workspace import Workspace
from ..config import ConfigManager
from ..metrics import BehaviorMetricsTracker
from ..constants import (
    EXTEND_MARKER,
    NEEDS_INPUT_MARKER,
    ROUTE_PREFIX,
    STATE_UPDATE_START, CMD_EXIT, CMD_HELP, CMD_CONTEXT, CMD_CONTEXT_EDIT, CMD_EDIT, CMD_FILE_PREFIX, CMD_TASK,
    USER_ROLE, MSG_CHAT_STARTED, MSG_SESSION_LOG, MSG_SESSION_STATUS, MSG_MIGRATION,
    MSG_MEMORY_SAVING, MSG_MEMORY_FAILED, MSG_SHUTDOWN,
    MSG_DOUBLE_PREFIX, MSG_EMPTY_INPUT,
    HANDOFF_SYNTHESIS_MSG,
)

_logger = logging.getLogger("quimera.staging")
_log_level = os.environ.get("QUIMERA_LOG_LEVEL", "INFO").upper()
_numeric_level = getattr(logging, _log_level, logging.INFO)


class _PromptAwareStderrHandler(logging.StreamHandler):
    """Clear and redraw the interactive prompt around staging logs."""

    def __init__(self, stream=None):
        super().__init__(stream or sys.stderr)
        self._app = None

    def bind_app(self, app) -> None:
        self._app = app

    def emit(self, record):
        app = self._app
        if app is None:
            super().emit(record)
            return

        stdin_is_tty = sys.stdin is not None and sys.stdin.isatty()
        if stdin_is_tty and self.stream is sys.stderr:
            self.stream = sys.stdout

        with app._output_lock:
            app._clear_user_prompt_line_if_needed()
            super().emit(record)
            self.flush()
            app._redisplay_user_prompt_if_needed()


_logger.setLevel(_numeric_level)
if not any(isinstance(handler, _PromptAwareStderrHandler) for handler in _logger.handlers):
    handler = _PromptAwareStderrHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _logger.addHandler(handler)
    _logger.propagate = False


class QuimeraApp:
    """Orquestra comandos locais, roteamento entre agentes e ciclo da sessão."""
    HANDOFF_PAYLOAD_PATTERN = re.compile(
        r"^\s*task:\s*([^\n]+?)\s*(?:(?:\n|\|\s*)context:\s*([^\n]+?))?\s*(?:(?:\n|\|\s*)expected:\s*([^\n]+?))?\s*(?:(?:\n|\|\s*)priority:\s*([^\n]+?))?\s*$",
        re.IGNORECASE,
    )
    STATE_UPDATE_PATTERN = re.compile(
        r"\[STATE_UPDATE\](.*?)\[/STATE_UPDATE\]", re.DOTALL
    )
    ROUTE_PATTERN = re.compile(r"\[ROUTE:([A-Za-z0-9_-]+)\]\s*([\s\S]+)", re.M | re.I)
    ACK_PATTERN = re.compile(r"^\s*\[ACK:([A-Za-z0-9]+)\]\s*", re.M)

    def __init__(self,
             cwd: Path,
             debug: bool = False,
             history_window: int | None = None,
             agents: list | None = None,
             threads: int = 1,
             timeout: int | None = None,
             idle_timeout_seconds: int | None = None,
             spy: bool = False
        ):
        selected_agents = list(agents) if agents else []
        self.active_agents = self._agents = selected_agents
        self.threads = int(threads) if threads is not None else 1
        self.agent_failures = defaultdict(int)
        self._agent_failures_lock = threading.Lock()
        self.renderer = TerminalRenderer()
        self.config = ConfigManager()
        self.user_name = self.config.user_name
        self.workspace = Workspace(cwd)
        self.spy = spy
        self.system_layer = AppSystemLayer(self)

        # Configuração do histórico persistente (readline)
        self.history_file = self.workspace.history_file
        if readline:
            if self.history_file.exists():
                try:
                    readline.read_history_file(str(self.history_file))
                except Exception:
                    pass
            readline.set_history_length(1000)

        migrated = self.workspace.migrate_from_legacy(cwd)
        for item in migrated:
            self.renderer.show_system(MSG_MIGRATION.format(item))

        self.context_manager = ContextManager(
            self.workspace.context_persistent,
            self.workspace.context_session,
            self.renderer,
        )
        self.storage = SessionStorage(self.workspace.logs_dir, self.renderer)
        session_id = self.storage.get_history_file().stem
        metrics_file = self.workspace.metrics_dir / f"{session_id}.jsonl" if debug else None
        self.agent_client = AgentClient(self.renderer, metrics_file=metrics_file, timeout=timeout, spy=self.spy, working_dir=str(self.workspace.cwd))
        self._create_task_executor = create_executor
        self.session_summarizer = SessionSummarizer(
            self.renderer,
            summarizer_call=build_chain_summarizer(self.agent_client, list(dict.fromkeys(["qwen"] + (self.active_agents or [])))),
        )
        self.summary_agent_preference = "qwen"
        self._pending_input_for: str | None = None
        last_session = self.storage.load_last_session()
        self.history = last_session["messages"]
        session_context = self.context_manager.load_session()
        history_restored = bool(self.history)
        summary_loaded = self.context_manager.SUMMARY_MARKER in session_context
        self.session_state = {
            "session_id": session_id,
            "history_count": len(self.history),
            "history_restored": history_restored,
            "summary_loaded": summary_loaded,
            "handoffs_sent": 0,
            "handoffs_received": 0,
            "handoffs_succeeded": 0,
            "handoffs_failed": 0,
            "total_latency": 0.0,
            "agent_metrics": {},
            "rounds_without_progress": 0,
            "consecutive_redundant_responses": 0,
            "handoff_invalid_count": 0,
            "responses_with_clear_next_step": 0,
            "total_responses": 0,
        }
        # Persist metrics state to workspace so agents can resume with previous metrics
        metrics_state_path = self.workspace.state_dir / "metrics_state.json"
        self.behavior_metrics = BehaviorMetricsTracker(storage_path=metrics_state_path)
        self.debug_prompt_metrics = debug
        self.round_index = 0
        self.session_call_index = 0
        self.shared_state = last_session["shared_state"]
        self._lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._nonblocking_prompt_visible = False
        self._nonblocking_prompt_text = ""
        self._nonblocking_input_thread: threading.Thread | None = None
        self._nonblocking_input_queue: "queue.Queue | None" = None
        self._nonblocking_input_status = "idle"
        for handler in _logger.handlers:
            if isinstance(handler, _PromptAwareStderrHandler):
                handler.bind_app(self)
        is_new_session = not history_restored and not summary_loaded

        # Unify tasks database path
        self.tasks_db_path = str(self.workspace.tasks_db)
        runtime_tasks.init_db(self.tasks_db_path)
        self.current_job_id = runtime_tasks.add_job(f"Session {session_id}", db_path=self.tasks_db_path)
        self.session_state["current_job_id"] = self.current_job_id
        os.environ["QUIMERA_CURRENT_JOB_ID"] = str(self.current_job_id)

        session_state = {
            "session_id": self.session_state["session_id"],
            "is_new_session": self._format_yes_no(is_new_session),
            "history_restored": self._format_yes_no(history_restored),
            "summary_loaded": self._format_yes_no(summary_loaded),
            "current_job_id": self.current_job_id,
        }
        self.prompt_builder = PromptBuilder(
            self.context_manager,
            history_window=history_window or self.config.history_window,
            session_state=session_state,
            user_name=self.user_name,
            active_agents=self.active_agents,
            metrics_tracker=self.behavior_metrics,
        )
        self.auto_summarize_threshold = self.config.auto_summarize_threshold
        self.idle_timeout_seconds = idle_timeout_seconds if idle_timeout_seconds is not None else self.config.idle_timeout_seconds

        self.tool_executor = ToolExecutor(
            config=ToolRuntimeConfig(
                workspace_root=self.workspace.cwd,
                db_path=Path(self.tasks_db_path) if self.tasks_db_path else None,
                require_approval_for_mutations=False,
            ),
            approval_handler=ConsoleApprovalHandler(),
        )
        # Injeta o executor nos drivers de API do agent_client.
        self.agent_client.tool_executor = self.tool_executor
        # Set up task executors for autonomous task execution
        self._setup_task_executors()

    @staticmethod
    def _format_yes_no(value):
        return "sim" if value else "não"

    def _get_system_layer(self) -> AppSystemLayer:
        layer = getattr(self, "system_layer", None)
        if layer is None:
            layer = AppSystemLayer(self)
            self.system_layer = layer
        return layer

    def __del__(self):
        try:
            self._stop_task_executors()
        except Exception:
            pass

    def _record_failure(self, agent):
        with self._agent_failures_lock:
            self.agent_failures[agent] += 1
            failures = self.agent_failures[agent]
        if failures >= 2:
            if agent in self.active_agents:
                self.active_agents.remove(agent)
                _logger.warning("agent %s removed after %d failures", agent, failures)
                try:
                    runtime_tasks.release_agent_tasks(agent, db_path=self.tasks_db_path)
                except Exception:
                    pass
        self._record_agent_metric(agent, "failed", 0)

    @staticmethod
    def _unique_encodings(*encodings):
        seen = set()
        result = []
        for encoding in encodings:
            if not encoding:
                continue
            normalized = str(encoding).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(normalized)
        return result

    def _setup_task_executors(self):
        """Set up task executors for explicit human-created task execution."""
        app_tasks.setup_task_executors(self)

    def _stop_task_executors(self):
        app_tasks.stop_task_executors(self)

    def _build_task_overview(self) -> dict:
        return app_tasks.build_task_overview(self)

    def _task_context_history_window(self) -> int:
        return app_tasks.task_context_history_window(self)

    def _format_task_chat_context(self) -> str:
        return app_tasks.format_task_chat_context(self)

    def _build_task_body(self, description: str) -> str:
        return app_tasks.build_task_body(self, description)

    def _refresh_task_shared_state(self) -> None:
        app_tasks.refresh_task_shared_state(self)

    def _redisplay_user_prompt_if_needed(self) -> None:
        stdin = sys.stdin
        if stdin is None or not stdin.isatty():
            return
        if self._nonblocking_input_status != "reading":
            return
        try:
            import time
            time.sleep(0.01)
            prompt = getattr(self, "_nonblocking_prompt_text", "")
            line_buffer = ""
            if readline is not None:
                try:
                    line_buffer = readline.get_line_buffer()
                except Exception:
                    line_buffer = ""
            full_line = f"{prompt}{line_buffer}"
            if len(full_line) > 0:
                self._clear_user_prompt_line_if_needed()
                sys.stdout.write(full_line)
                sys.stdout.flush()
                if readline is not None:
                    try:
                        readline.redisplay()
                    except Exception:
                        pass
        except Exception:
            pass

    def _clear_user_prompt_line_if_needed(self) -> None:
        stdin = sys.stdin
        if stdin is None or not stdin.isatty():
            return
        if self._nonblocking_input_status != "reading":
            return
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

    def show_system_message(self, message: str) -> None:
        self._get_system_layer().show_system_message(message)

    def _show_task_response(self, task_id: int, agent: str, response: str) -> None:
        """Display the actual agent response for a task execution as a system message."""
        self._get_system_layer().show_task_response(task_id, agent, response)

    def resolve_agent_response(
        self,
        agent: str,
        response: str | None,
        silent: bool = False,
        persist_history: bool = True,
        show_output: bool = True,
    ) -> str | None:
        current_response = response
        max_tool_hops = 8
        tool_history = []

        for _ in range(max_tool_hops):
            if not current_response:
                return current_response

            raw_response, tool_result = self.tool_executor.maybe_execute_from_response(current_response)

            if tool_result is None:
                return current_response

            # Truncate tool result to reduce verbosity
            tool_payload = app_tasks.truncate_payload(tool_result.to_model_payload())
            
            tool_history.append(
                f"Sua resposta anterior:\n{current_response.strip()}\n\n"
                f"Resultado da ferramenta:\n{tool_payload}"
            )

            visible_text = strip_tool_block(raw_response or "")
            if visible_text:
                if show_output:
                    self.print_response(agent, visible_text)
                if persist_history:
                    self.persist_message(agent, visible_text)

            followup_handoff = (
                "Histórico de ferramentas desta rodada:\n\n"
                + "\n\n---\n\n".join(tool_history)
                + "\n\nContinue a partir daqui. Se precisar de outra ferramenta, emita nova tag <tool ... />."
            )

            current_response = self._call_agent(
                agent,
                handoff=followup_handoff,
                primary=False,
                protocol_mode="tool_loop",
                silent=silent,
            )

        return "Falha: limite de execuções de ferramenta atingido."

    def handle_command(self, user_input):
        return self._get_system_layer().handle_command(user_input)

    @staticmethod
    def _parse_task_command(command: str) -> str:
        return app_tasks.parse_task_command(command, CMD_TASK)

    def _get_task_routing_plugins(self):
        return app_tasks.get_task_routing_plugins(self)

    @staticmethod
    def _classify_task_execution_result(response: str | None) -> tuple[bool, str]:
        """Return whether the task execution can be considered completed."""
        return app_tasks.classify_task_execution_result(response)

    def _count_agent_open_tasks(self, agent_name: str) -> int:
        return app_tasks.count_agent_open_tasks(self, agent_name)

    def _choose_agent_with_load_balance(self, task_type: str) -> str | None:
        """Choose best agent for task_type, applying open-task penalty to avoid monopolies."""
        return app_tasks.choose_agent_with_load_balance(self, task_type)

    def _handle_task_command(self, command: str) -> None:
        app_tasks.handle_task_command(self, command, CMD_TASK)

    def read_user_input(self, prompt, timeout: int) -> str | None:
        return app_input.read_user_input(self, prompt, timeout, input_fn=input)

    def read_from_editor(self):
        return app_input.read_from_editor(self)

    def read_from_file(self, path_str):
        return app_input.read_from_file(self, path_str)

    def parse_routing(self, user_input):
        """Extrai o agente inicial e rejeita prefixos duplicados na mesma entrada.

        Retorna (agent, message, explicit) onde explicit=True indica que o usuário
        usou /claude ou /codex explicitamente.
        """
        stripped = user_input.lstrip()
        lowered = stripped.lower()

        active_plugins = []
        for n in self.active_agents:
            plugin = plugins.get(n)
            if plugin is not None:
                active_plugins.append(plugin)
        for p in active_plugins:
            prefix, agent = p.prefix, p.name
            if lowered == prefix:
                return agent, "", True
            if lowered.startswith(f"{prefix} "):
                message = stripped[len(prefix):].lstrip()
                lowered_message = message.lower()
                other_prefixes = [op.prefix for op in active_plugins if op.prefix != prefix]
                if any(lowered_message == op or lowered_message.startswith(f"{op} ") for op in other_prefixes):
                    self.renderer.show_warning(MSG_DOUBLE_PREFIX)
                    return None, None, False
                return agent, message, True

        if not self.active_agents:
            _logger.warning("no active agents, resetting to default")
            self.active_agents = self._agents
        return random.choice(self.active_agents), user_input, False

    @staticmethod
    def _merge_state_value(current, incoming):
        if incoming is None:
            return current
        if incoming == "":
            return None
        if isinstance(current, list) and isinstance(incoming, list):
            merged = current.copy()
            for item in incoming:
                if item not in merged:
                    merged.append(item)
            return merged
        return incoming

    def _apply_state_update(self, block_content):
        try:
            payload = json.loads(block_content.strip())
        except json.JSONDecodeError:
            return False

        if not isinstance(payload, dict):
            return False

        with self._lock:
            for key, value in payload.items():
                normalized_key = str(key).strip().lower().replace(" ", "_")
                if not normalized_key:
                    continue
                current = self.shared_state.get(normalized_key)
                merged = self._merge_state_value(current, value)
                if merged is None:
                    self.shared_state.pop(normalized_key, None)
                else:
                    self.shared_state[normalized_key] = merged
        return True

    MAX_RETRIES = 2
    RETRY_BACKOFF_SECONDS = 1

    def call_agent(self, agent, **options):
        dispatch_options = dict(options)
        silent = dispatch_options.pop("silent", False)
        persist_history = dispatch_options.pop("persist_history", True)
        show_output = dispatch_options.pop("show_output", True)
        handoff = dispatch_options.get("handoff")
        handoff_id = handoff.get("handoff_id") if isinstance(handoff, dict) else None
        _logger.info(
            "[DISPATCH] sending to agent=%s, handoff_only=%s, handoff_id=%s",
            agent, dispatch_options.get("handoff_only", False), handoff_id,
        )
        last_error = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self._call_agent(agent, silent=silent, **dispatch_options)
                if response is None:
                    if attempt < self.MAX_RETRIES:
                        _logger.warning("[DISPATCH] retry %d/%d for agent=%s", attempt, self.MAX_RETRIES, agent)
                        time.sleep(self.RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    self._record_failure(agent)
                    return None
                result = self.resolve_agent_response(
                    agent,
                    response,
                    silent=silent,
                    persist_history=persist_history,
                    show_output=show_output,
                )
                if result is None:
                    if attempt < self.MAX_RETRIES:
                        _logger.warning("[DISPATCH] retry %d/%d for agent=%s (resolve failed)", attempt, self.MAX_RETRIES, agent)
                        time.sleep(self.RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    self._record_failure(agent)
                return result
            except Exception as exc:
                last_error = exc
                if attempt < self.MAX_RETRIES:
                    _logger.warning("[DISPATCH] retry %d/%d for agent=%s after exception: %s", attempt, self.MAX_RETRIES, agent, exc)
                    time.sleep(self.RETRY_BACKOFF_SECONDS * attempt)
                    continue
                self._record_failure(agent)
                raise
        if last_error:
            _logger.error("[DISPATCH] all retries exhausted for agent=%s", agent)
        return None

    def _call_agent(self, agent, is_first_speaker=False, handoff=None, primary=True, protocol_mode="standard", handoff_only=False, silent=False, from_agent=None):
        with self._counter_lock:
            self.session_call_index += 1
            call_index_snapshot = self.session_call_index
        start = time.time()
        history = [] if handoff_only else self.history
        self._refresh_task_shared_state()
        # Agentes com driver de API recebem tools via schema OpenAI — as instruções
        # text-based conflitariam com o protocolo da API e devem ser omitidas.
        plugin = plugins.get(agent)
        _driver = getattr(plugin, "driver", "cli") if plugin else "cli"
        skip_tool_prompt = isinstance(_driver, str) and _driver != "cli"
        if self.debug_prompt_metrics:
            prompt, metrics = self.prompt_builder.build(
                agent,
                history,
                is_first_speaker,
                handoff,
                debug=True,
                primary=primary,
                shared_state=self.shared_state,
                handoff_only=handoff_only,
                from_agent=from_agent,
                skip_tool_prompt=skip_tool_prompt,
            )
            self.agent_client.log_prompt_metrics(
                agent, metrics,
                session_id=self.session_state["session_id"],
                round_index=self.round_index,
                session_call_index=call_index_snapshot,
                history_window=self.prompt_builder.history_window,
                protocol_mode=protocol_mode,
            )
        else:
            prompt = self.prompt_builder.build(
                agent, history, is_first_speaker, handoff,
                primary=primary, shared_state=self.shared_state,
                handoff_only=handoff_only, from_agent=from_agent,
                skip_tool_prompt=skip_tool_prompt,
            )
        result = self.agent_client.call(agent, prompt, silent=silent)
        elapsed = time.time() - start
        if hasattr(self, 'session_state') and self.session_state:
            with self._counter_lock:
                try:
                    self.session_state["handoffs_sent"] += 1
                    self.session_state["total_latency"] += elapsed
                    if result:
                        self.session_state["handoffs_succeeded"] += 1
                    else:
                        self.session_state["handoffs_failed"] += 1
                except KeyError:
                    pass  # Old session state without metrics
            self._record_agent_metric(agent, "succeeded" if result else "failed", elapsed)
        _logger.info("[DISPATCH] agent=%s latency=%.2fs result=%s", agent, elapsed, "ok" if result else "none")
        return result

    _PAYLOAD_FIELD_RE = re.compile(r"^\s*(task|context|expected)\s*:", re.IGNORECASE)

    @staticmethod
    def _strip_payload_residual(text):
        """Remove trailing non-payload lines from captured ROUTE group."""
        if not text:
            return ""
        _logger.debug("[ROUTE] raw_payload before strip: %r", text)
        kept = []
        for line in text.splitlines():
            if QuimeraApp._PAYLOAD_FIELD_RE.match(line) or (kept and not line.strip()):
                kept.append(line)
            else:
                break
        result = "\n".join(kept).strip()
        _logger.debug("[ROUTE] raw_payload after strip: %r", result)
        return result

    def _record_agent_metric(self, agent, metric_name, latency):
        """Track per-agent handoff metrics."""
        metrics = self.session_state.get("agent_metrics", {})
        if agent not in metrics:
            metrics[agent] = {"sent": 0, "received": 0, "succeeded": 0, "failed": 0, "latency": 0.0}
        if metric_name == "sent":
            metrics[agent]["sent"] += 1
        elif metric_name == "received":
            metrics[agent]["received"] += 1
        elif metric_name == "succeeded":
            metrics[agent]["succeeded"] += 1
        elif metric_name == "failed":
            metrics[agent]["failed"] += 1
        if latency:
            metrics[agent]["latency"] += latency
        self.session_state["agent_metrics"] = metrics

        if hasattr(self, 'behavior_metrics') and self.behavior_metrics:
            if metric_name in ("succeeded", "failed"):
                self.behavior_metrics.record_response(
                    agent, latency,
                    has_next_step=metric_name == "succeeded",
                    is_empty=metric_name == "failed",
                )

    def _has_clear_next_step(self, response):
        """Detecta se a resposta indica próximo passo claro."""
        if not response:
            return False
        response_lower = response.lower()
        indicators = [
            "próximo passo",
            "próxima etapa",
            "avançar",
            "continuar com",
            "a seguir",
            "para continuar",
            "próxima ação",
            "tarefa completa",
            "finalizado",
            "concluído",
            "done",
            "next step",
            "continuando",
        ]
        return any(ind in response_lower for ind in indicators)

    def _is_response_redundant(self, response, history):
        """Detecta se a resposta é redundante comparada ao histórico recente."""
        if not response or len(history) < 2:
            return False
        response_clean = response.lower().strip()
        recent_responses = [m["content"].lower().strip() for m in history[-3:] if m.get("role") != "human"]
        for past in recent_responses:
            if past and len(past) > 50 and len(response_clean) > 50:
                from difflib import SequenceMatcher
                similarity = SequenceMatcher(None, past, response_clean).ratio()
                if similarity > 0.7:
                    return True
        return False

    @staticmethod
    def _generate_handoff_id(task, target, timestamp=None):
        """Generate a deterministic ID for a handoff based on task content and target."""
        ts = timestamp or time.time()
        raw = f"{ts}:{target}:{task}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def parse_handoff_payload(self, payload, target=None):
        if not payload:
            return None
        match = self.HANDOFF_PAYLOAD_PATTERN.match(payload.strip())
        if not match:
            _logger.warning(f"[HANDOFF] Payload did not match regex: {payload!r}")
            return None

        groups = match.groups()
        task, context, expected = (groups[i].strip() if groups[i] else None for i in range(3))
        priority_raw = groups[3].strip() if len(groups) > 3 and groups[3] else None
        priority = priority_raw.lower() if priority_raw else "normal"
        if priority not in ("normal", "urgent", "low"):
            priority = "normal"

        if not task:
            _logger.warning(f"[HANDOFF] Missing required field 'task' - got task={task!r}, context={context!r}, expected={expected!r}")
            return None

        handoff_id = self._generate_handoff_id(task, target or "unknown")

        return {
            "task": task,
            "context": context,
            "expected": expected,
            "priority": priority,
            "handoff_id": handoff_id,
            "chain": [],
        }

    def parse_response(self, response):
        """Extrai marcadores de controle e retorna (clean, route_target, handoff, extend, needs_human_input, ack_id)."""
        if response is None:
            return None, None, None, False, False, None

        route_target, handoff, ack_id = None, None, None

        if STATE_UPDATE_START in response:
            for state_match in self.STATE_UPDATE_PATTERN.finditer(response):
                self._apply_state_update(state_match.group(1))
            response = self.STATE_UPDATE_PATTERN.sub("", response).strip()

        # Extract ACK marker
        ack_match = self.ACK_PATTERN.search(response)
        if ack_match:
            ack_id = ack_match.group(1)
            response = self.ACK_PATTERN.sub("", response, count=1).strip()
            _logger.info("[ACK] received ack_id=%s", ack_id)

        if ROUTE_PREFIX in response:
            match = self.ROUTE_PATTERN.search(response)
            if match:
                raw_payload = self._strip_payload_residual(match.group(2))
                route_target = match.group(1)
                parsed_handoff = self.parse_handoff_payload(raw_payload, target=route_target)
                _logger.info("[ROUTE] match=%s, target=%s", match.group(0)[:100], route_target)
                if parsed_handoff:
                    handoff = parsed_handoff
                    if hasattr(self, 'session_state') and self.session_state:
                        try:
                            self.session_state["handoffs_received"] += 1
                        except KeyError:
                            pass  # Old session state without metrics
                else:
                    _logger.warning("[ROUTE] handoff parse failed for target=%s, payload: %r", route_target, raw_payload)
                    if hasattr(self, 'session_state') and self.session_state:
                        try:
                            self.session_state["handoff_invalid_count"] = self.session_state.get("handoff_invalid_count", 0) + 1
                        except KeyError:
                            pass
                    if hasattr(self, 'behavior_metrics') and self.behavior_metrics:
                        self.behavior_metrics.record_handoff_sent(route_target, is_invalid=True)
                    route_target = None  # Reset target if handoff parse fails
                response = self.ROUTE_PATTERN.sub("", response, count=1).strip() or None

        extend = response.rstrip().endswith(EXTEND_MARKER)
        if extend:
            response = response.rstrip()[: -len(EXTEND_MARKER)].rstrip()

        needs_human_input = NEEDS_INPUT_MARKER in response
        if needs_human_input:
            response = response.replace(NEEDS_INPUT_MARKER, "").strip()

        return response, route_target, handoff, extend, needs_human_input, ack_id

    def _call_agent_for_parallel(self, agent, handoff, protocol_mode, staging_root, index):
        """Executa call_agent e retorna tupla (agent, response, route_target, handoff, extend, needs_input)."""
        from ..runtime.tools.files import set_staging_root
        
        set_staging_root(staging_root / str(index))
        try:
            raw = self.call_agent(agent, handoff=handoff, primary=False, protocol_mode=protocol_mode)
            response, route_target, handoff, extend, needs_input, _ = self.parse_response(raw)
            # Propaga o flag de necessidade de input humano (6º elemento)
            return agent, response, route_target, handoff, extend, needs_input
        finally:
            set_staging_root(None)

    def _merge_staging_to_workspace(self, staging_root: Path):
        """Mescla arquivos do staging para o workspace em ordem de índice."""
        
        if not staging_root.exists():
            _logger.debug("merge: staging_root does not exist, skipping")
            return
        
        index_dirs = sorted(staging_root.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 999)
        total_merged = 0
        
        for index_dir in index_dirs:
            if not index_dir.is_dir():
                continue
            for src in index_dir.rglob("*"):
                if not src.is_file():
                    continue
                rel_path = src.relative_to(index_dir)
                dest = self.workspace.cwd / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                total_merged += 1
                _logger.debug("merged: %s -> %s", src, dest)
        
        _logger.info("merge completed: %d files to %s", total_merged, self.workspace.cwd)

    def print_response(self, agent, response):
        with self._output_lock:
            if response is not None:
                self.renderer.show_message(agent, response)
            else:
                self.renderer.show_no_response(agent)

    def persist_message(self, role, content):
        """Persiste uma mensagem no histórico em memória, log e snapshot JSON."""
        with self._lock:
            self.history.append({"role": role, "content": content})
            self.storage.append_log(role, content)
            self.storage.save_history(self.history, shared_state=self.shared_state)
            if hasattr(self, 'session_state') and self.session_state and role != "human":
                try:
                    self.session_state["total_responses"] = self.session_state.get("total_responses", 0) + 1
                    has_next = self._has_clear_next_step(content)
                    is_redundant = self._is_response_redundant(content, self.history)
                    is_empty = not content or not content.strip()
                    if has_next:
                        self.session_state["responses_with_clear_next_step"] = self.session_state.get("responses_with_clear_next_step", 0) + 1
                    if is_redundant:
                        self.session_state["consecutive_redundant_responses"] = self.session_state.get("consecutive_redundant_responses", 0) + 1
                    else:
                        self.session_state["consecutive_redundant_responses"] = 0
                    # Integra com BehaviorMetricsTracker
                    if hasattr(self, 'behavior_metrics') and self.behavior_metrics:
                        self.behavior_metrics.record_response(
                            role, 0.0,
                            has_next_step=has_next,
                            is_empty=is_empty,
                            is_redundant=is_redundant,
                        )
                except KeyError:
                    pass

    def _maybe_auto_summarize(self, preferred_agent=None):
        """Sumariza e trunca o histórico quando excede o threshold configurado."""
        threshold = getattr(self, "auto_summarize_threshold", None)
        if not isinstance(threshold, int) or threshold <= 0:
            return
        if len(self.history) < threshold:
            return

        keep = self.prompt_builder.history_window
        to_summarize = self.history[:-keep]
        recent = self.history[-keep:]
        existing_summary = self.context_manager.load_session_summary()

        self.renderer.show_system(
            f"[memória] histórico com {len(self.history)} mensagens — gerando resumo automático..."
        )
        summary_agent_preference = preferred_agent or getattr(
            self,
            "summary_agent_preference",
            self.active_agents[0],
        )
        summary = self.session_summarizer.summarize(
            to_summarize,
            existing_summary=existing_summary,
            preferred_agent=summary_agent_preference,
        )
        if summary:
            self.context_manager.update_with_summary(summary)
            self.history = recent
            self.storage.save_history(self.history, shared_state=self.shared_state)
            self.renderer.show_system(
                f"[memória] histórico truncado para {len(self.history)} mensagens recentes"
            )
        else:
            self.renderer.show_system("[memória] resumo automático falhou — histórico mantido")

    def shutdown(self):
        """Finaliza a sessão tentando resumir o histórico no contexto persistente."""
        self._stop_task_executors()
        if readline:
            try:
                readline.write_history_file(str(self.history_file))
            except Exception:
                pass

        if not self.history:
            return

        self.renderer.show_system(MSG_MEMORY_SAVING)

        summary = self.session_summarizer.summarize(
            self.history,
            existing_summary=self.context_manager.load_session_summary(),
            preferred_agent=getattr(self, "summary_agent_preference", None),
        )
        if summary:
            self.context_manager.update_with_summary(summary)
        else:
            self.renderer.show_system(MSG_MEMORY_FAILED)

    def _process_chat_message(self, user):
        first_agent, message, explicit = self.parse_routing(user)
        if first_agent is None:
            return
        if not message or not message.strip():
            self.renderer.show_warning(MSG_EMPTY_INPUT.format(first_agent))
            return

        # Se há um agente aguardando resposta humana e o usuário não especificou
        # explicitamente outro agente, redireciona para ele
        pending_input_for = getattr(self, "_pending_input_for", None)
        if pending_input_for and not explicit:
            first_agent = pending_input_for
        self._pending_input_for = None

        other_agents = [n for n in self.active_agents if n != first_agent]

        self.round_index += 1
        self.summary_agent_preference = first_agent
        self.persist_message(USER_ROLE, message)

        # Primeira fala: detecta roteamento ou debate estendido
        response = self.call_agent(first_agent, is_first_speaker=True, protocol_mode="standard")
        response, route_target, handoff, extend, needs_human_input, _ = self.parse_response(response)

        if needs_human_input:
            if response:
                self.renderer.show_message(first_agent, response)
            self._pending_input_for = first_agent
            self.renderer.show_system(f"\nResponda para {first_agent.upper()}:\n")
            return
        self.print_response(first_agent, response)
        if response is not None:
            self.persist_message(first_agent, response)

        # Um handoff emitido pela primeira resposta sempre tem prioridade,
        # inclusive quando a rodada começou com /claude ou /codex.
        if route_target and handoff:
            handoff_id = handoff.get("handoff_id", "?")
            priority = handoff.get("priority", "normal")

            # Verifica delegação circular
            chain = handoff.get("chain", []) if isinstance(handoff, dict) else []
            if route_target in chain:
                _logger.warning(
                    "[HANDOFF] Circular delegation detected: %s -> %s (chain: %s)",
                    first_agent, route_target, chain,
                )
                if hasattr(self, 'behavior_metrics') and self.behavior_metrics:
                    self.behavior_metrics.record_handoff_received(route_target, is_circular=True)
                self.renderer.show_warning(
                    f"Delegação circular detectada: {first_agent} -> {route_target}. "
                    f"Cadeia: {' -> '.join(chain + [route_target])}"
                )
                return

            self.renderer.show_handoff(
                first_agent,
                route_target,
                task=handoff["task"],
            )
            _logger.info(
                "[HANDOFF] id=%s from=%s to=%s priority=%s chain=%s",
                handoff_id, first_agent, route_target, priority, chain,
            )
            # Propaga a cadeia de handoffs
            if isinstance(handoff, dict):
                handoff["chain"] = chain + [first_agent]
            # Registra métricas de handoff
            if hasattr(self, 'behavior_metrics') and self.behavior_metrics:
                self.behavior_metrics.record_handoff_sent(first_agent)
                self.behavior_metrics.record_handoff_received(route_target)
            # Handoff v1: agente secundário recebe apenas o payload delegado
            secondary_response = self.call_agent(
                route_target,
                handoff=handoff,
                handoff_only=True,
                primary=False,
                protocol_mode="handoff",
                from_agent=first_agent,
            )
            expected_ack = handoff.get("handoff_id")
            secondary_response, _, _, _, _, ack_id = self.parse_response(secondary_response)
            if expected_ack and ack_id and ack_id != expected_ack:
                _logger.warning(
                    "[ACK] mismatch: expected=%s, received=%s from agent=%s",
                    expected_ack, ack_id, route_target,
                )
            self.print_response(route_target, secondary_response)
            if secondary_response is not None:
                self.persist_message(route_target, secondary_response)

            # Fallback chain: se o agente secundário não respondeu, tenta próximo disponível
            if not secondary_response:
                fallback_candidates = [
                    a for a in self.active_agents
                    if a != first_agent and a != route_target and a not in chain
                ]
                for fallback_agent in fallback_candidates:
                    _logger.info(
                        "[HANDOFF] id=%s fallback: trying %s after %s failed",
                        handoff_id, fallback_agent, route_target,
                    )
                    self.renderer.show_system(
                        f"[handoff] tentando fallback: {fallback_agent} (após {route_target} falhar)"
                    )
                    fallback_handoff = dict(handoff) if isinstance(handoff, dict) else handoff
                    if isinstance(fallback_handoff, dict):
                        fallback_handoff["chain"] = handoff.get("chain", []) + [route_target]
                    secondary_response = self.call_agent(
                        fallback_agent,
                        handoff=fallback_handoff,
                        handoff_only=True,
                        primary=False,
                        protocol_mode="handoff",
                        from_agent=first_agent,
                    )
                    secondary_response, _, _, _, _, ack_id = self.parse_response(secondary_response)
                    if secondary_response:
                        route_target = fallback_agent
                        self.print_response(fallback_agent, secondary_response)
                        self.persist_message(fallback_agent, secondary_response)
                        break

            # Integrador: agente primário sintetiza com a resposta do secundário
            if secondary_response:
                synthesis_handoff = HANDOFF_SYNTHESIS_MSG.format(
                    agent=route_target.upper(),
                    task=handoff["task"],
                    response=secondary_response,
                )
                # Registra síntese no BehaviorMetricsTracker
                if hasattr(self, 'behavior_metrics') and self.behavior_metrics:
                    self.behavior_metrics.record_synthesis(first_agent)
                final_response = self.call_agent(
                    first_agent,
                    handoff=synthesis_handoff,
                    primary=False,
                    protocol_mode="handoff",
                )
                final_response, _, _, _, _, _ = self.parse_response(final_response)
                self.print_response(first_agent, final_response)
                if final_response is not None:
                    self.persist_message(first_agent, final_response)
            else:
                _logger.warning(
                    "[HANDOFF] id=%s failed: secondary agent %s returned no response",
                    handoff_id, route_target,
                )
                self.renderer.show_system(
                    f"[handoff] {route_target} não respondeu — delegação falhou"
                )
        else:
            # Fluxo padrão: 2 falas. Estendido (EXTEND_MARKER): 4 falas alternadas.
            # Em rodadas com /claude ou /codex, o handoff do primeiro agente
            # já foi tratado no bloco acima. Aqui só decidimos se existe
            # continuação automática do fluxo normal.
            protocol_mode = "extended" if extend else "standard"
            if explicit:
                remaining = []
            elif extend:
                remaining = [other_agents[0], first_agent, other_agents[0]] if other_agents else []
            else:
                remaining = other_agents

            next_handoff = None
            if self.threads > 1 and len(remaining) > 1:
                # Modo paralelo: executar agentes em paralelo
                staging_root = Path(tempfile.gettempdir()) / "quimera-staging" / f"{self.session_state['session_id']}-round{self.round_index}"
                staging_root.mkdir(parents=True, exist_ok=True)
                _logger.info("parallel mode: %d threads, staging=%s", self.threads, staging_root)
                try:
                    with ThreadPoolExecutor(max_workers=self.threads) as executor:
                        # Criar lista de (agent, handoff, staging_dir, index)
                        agent_handoff_pairs = [(agent, None, staging_root, i) for i, agent in enumerate(remaining)]
                        # Executar em paralelo
                        futures = [
                            executor.submit(
                                self._call_agent_for_parallel,
                                agent,
                                handoff,
                                protocol_mode,
                                staging_dir,
                                idx,
                            )
                            for agent, handoff, staging_dir, idx in agent_handoff_pairs
                        ]
                        results = [f.result() for f in futures]
                    # Merge ordenado para o workspace
                    self._merge_staging_to_workspace(staging_root)
                    # Processar resultados na ordem original
                    needs_input_any = False
                    # Expecting 6-tuple now: (agent, response, route_target, handoff, extend, needs_input)
                    for item in results:
                        agent, response, route_target, handoff, extend, needs_input = item
                        self.print_response(agent, response)
                        if response is not None:
                            self.persist_message(agent, response)
                        needs_input_any = needs_input or needs_input_any
                    # Nota: route_target e handoff podem ser ignorados no modo paralelo
                    if needs_input_any:
                        needing = next((a for a in results if a[-1]), None)
                        if needing:
                            current_agent = needing[0]
                            self._pending_input_for = current_agent
                            self.renderer.show_system(f"\nResponda para {current_agent.upper()}:\n")
                except Exception as exc:
                    _logger.exception("parallel stage failed: %s", exc)
                    raise
                finally:
                    if staging_root.exists():
                        shutil.rmtree(staging_root)
                        _logger.info("staging cleanup: %s removed", staging_root)
            else:
                # Modo sequencial (original)
                for index, agent in enumerate(remaining):
                    response = self.call_agent(agent, handoff=next_handoff, primary=False, protocol_mode=protocol_mode)
                    next_handoff = None
                    response, route_target, handoff, _, needs_human_input, _ = self.parse_response(response)
                    self.print_response(agent, response)
                    if response is not None:
                        self.persist_message(agent, response)
                    if needs_human_input:
                        self._pending_input_for = agent
                        self.renderer.show_system(f"\nResponda para {agent.upper()}:\n")
                        break
                    if route_target and index + 1 < len(remaining):
                        remaining[index + 1] = route_target
                    if route_target:
                        next_handoff = handoff

        self._maybe_auto_summarize(preferred_agent=first_agent)

    def _process_chat_queue(self, chat_queue: queue.Queue):
        while True:
            user = chat_queue.get()
            try:
                if user is None:
                    return
                self._process_chat_message(user)
            finally:
                chat_queue.task_done()

    def run(self):
        """Executa o loop interativo do chat multiagente."""
        self.renderer.show_system(MSG_CHAT_STARTED)
        self.renderer.show_system(
            MSG_SESSION_STATUS.format(
                session_id=self.session_state["session_id"],
                history_count=self.session_state["history_count"],
                summary_loaded=self._format_yes_no(self.session_state["summary_loaded"]),
            )
        )
        self.renderer.show_system(MSG_SESSION_LOG.format(self.storage.get_log_file()))

        threaded_chat = self.threads > 1
        chat_queue = None
        chat_worker = None
        if threaded_chat:
            chat_queue = queue.Queue()
            chat_worker = threading.Thread(
                target=self._process_chat_queue,
                args=(chat_queue,),
                daemon=True,
            )
            chat_worker.start()

        try:
            while True:
                user = self.read_user_input(f"{self.user_name}: ", timeout=0)
                if user is None:
                    if not sys.stdin.isatty():
                        break
                    continue

                if user == CMD_EXIT:
                    break

                if user.strip() == CMD_EDIT:
                    content = self.read_from_editor()
                    if not content:
                        continue
                    user = content

                elif user.strip().startswith(CMD_FILE_PREFIX):
                    path_str = user.strip()[len(CMD_FILE_PREFIX):]
                    content = self.read_from_file(path_str)
                    if not content:
                        continue
                    user = content

                if self.handle_command(user):
                    continue

                if chat_queue is not None:
                    chat_queue.put(user)
                else:
                    self._process_chat_message(user)
        except KeyboardInterrupt:
            self.renderer.show_system(MSG_SHUTDOWN)
        finally:
            if threaded_chat and chat_queue is not None:
                chat_queue.put(None)
                chat_queue.join()
            if chat_worker is not None:
                chat_worker.join(timeout=5)
            self.shutdown()
