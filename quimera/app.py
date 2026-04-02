import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

try:
    import readline
except ImportError:
    readline = None

from .runtime.executor import ToolExecutor
from .runtime.parser import strip_tool_block
from .runtime import ToolRuntimeConfig, ConsoleApprovalHandler, TaskExecutor, create_executor
from .runtime.tasks import init_db
from .ui import TerminalRenderer
from .context import ContextManager
from .storage import SessionStorage
from .agents import AgentClient
from .session_summary import SessionSummarizer, build_chain_summarizer
from .prompt import PromptBuilder
from .workspace import Workspace
from .config import ConfigManager
from . import plugins
from .constants import (
    build_help,
    EXTEND_MARKER,
    NEEDS_INPUT_MARKER,
    ROUTE_PREFIX,
    STATE_UPDATE_START, CMD_EXIT, CMD_HELP, CMD_CONTEXT, CMD_CONTEXT_EDIT, CMD_EDIT, CMD_FILE_PREFIX,
    USER_ROLE, MSG_CHAT_STARTED, MSG_SESSION_LOG, MSG_SESSION_STATUS, MSG_MIGRATION,
    MSG_MEMORY_SAVING, MSG_MEMORY_FAILED, MSG_SHUTDOWN,
    MSG_DOUBLE_PREFIX, MSG_EMPTY_INPUT,
    HANDOFF_SYNTHESIS_MSG,
)

_logger = logging.getLogger("quimera.staging")
if not _logger.handlers and not logging.getLogger().handlers:
    _logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _logger.addHandler(handler)


class QuimeraApp:
    """Orquestra comandos locais, roteamento entre agentes e ciclo da sessão."""
    HANDOFF_PAYLOAD_PATTERN = re.compile(
        r"^\s*task:\s*([^\n]+?)\s*(?:(?:\n|\|\s*)context:\s*([^\n]+?))?\s*(?:(?:\n|\|\s*)expected:\s*([^\n]+?))?\s*$",
        re.IGNORECASE,
    )
    STATE_UPDATE_PATTERN = re.compile(
        r"\[STATE_UPDATE\](.*?)\[/STATE_UPDATE\]", re.DOTALL
    )
    ROUTE_PATTERN = re.compile(r"\[ROUTE:([A-Za-z0-9_-]+)\]\s*([\s\S]+)", re.M | re.I)

    @staticmethod
    def _format_yes_no(value):
        return "sim" if value else "não"

    def _record_failure(self, agent):
        with self._agent_failures_lock:
            self.agent_failures[agent] += 1
            failures = self.agent_failures[agent]
        if failures >= 2:
            if agent in self.active_agents:
                self.active_agents.remove(agent)
                _logger.warning("agent %s removed after %d failures", agent, failures)

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
        self.active_agents = agents or ["*"]
        self.threads = int(threads) if threads is not None else 1
        self.agent_failures = defaultdict(int)
        self._agent_failures_lock = threading.Lock()
        self.renderer = TerminalRenderer()
        self.config = ConfigManager()
        self.user_name = self.config.user_name
        self.workspace = Workspace(cwd)
        self.spy = spy

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
        self.agent_client = AgentClient(self.renderer, metrics_file=metrics_file, timeout=timeout, spy=self.spy)
        self.session_summarizer = SessionSummarizer(
            self.renderer,
            summarizer_call=build_chain_summarizer(self.agent_client, list(dict.fromkeys(["qwen"] + self.active_agents))),
        )
        self.summary_agent_preference = "qwen"
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
        }
        self.debug_prompt_metrics = debug
        self.round_index = 0
        self.session_call_index = 0
        self.shared_state = last_session["shared_state"]
        self._lock = threading.Lock()
        is_new_session = not history_restored and not summary_loaded
        session_state = {
            "session_id": self.session_state["session_id"],
            "is_new_session": self._format_yes_no(is_new_session),
            "history_restored": self._format_yes_no(history_restored),
            "summary_loaded": self._format_yes_no(summary_loaded),
        }
        self.prompt_builder = PromptBuilder(
            self.context_manager,
            history_window=history_window or self.config.history_window,
            session_state=session_state,
            user_name=self.user_name,
        )
        self.auto_summarize_threshold = self.config.auto_summarize_threshold
        self.idle_timeout_seconds = idle_timeout_seconds if idle_timeout_seconds is not None else self.config.idle_timeout_seconds

        self.tool_executor = ToolExecutor(
            config=ToolRuntimeConfig(
                workspace_root=self.workspace.cwd,
                require_approval_for_mutations=False,
            ),
            approval_handler=None,  # type: ignore[assignment]
        )
        # Set up task executors for autonomous task execution
        self._setup_task_executors()

    def _setup_task_executors(self):
        """Set up task executors for autonomous task execution."""
        from .runtime.tasks import approve_task, complete_task, fail_task

        def make_task_handler(agent_name):
            def task_handler(task_dict):
                """Handle task execution - delegate to agent via chat."""
                try:
                    task_id = task_dict["id"]
                    description = task_dict.get("description", "")
                    body = task_dict.get("body", "") or description
                    
                    if not body:
                        fail_task(task_id, reason="empty body")
                        return False
                    
                    prompt = f"Execute a seguinte tarefa:\n\n{body}"
                    
                    response = self.call_agent(
                        agent_name,
                        handoff=prompt,
                        handoff_only=True,
                        primary=False,
                        protocol_mode="task_execution",
                    )
                    
                    complete_task(task_id, result=response)
                    return True
                except Exception as e:
                    fail_task(task_dict["id"], reason=str(e))
                    return False
            return task_handler
        
        self.task_executors = []
        db_path = str(self.workspace.root / "data" / "task.db")
        init_db(db_path)
        for agent in self.active_agents:
            executor = create_executor(agent, make_task_handler(agent), db_path=db_path)
            executor.start()
            self.task_executors.append(executor)

    def _truncate_tool_result(self, content: str, max_lines: int = 10) -> str:
        """Truncate tool result content to max_lines lines."""
        if not content:
            return content
        lines = content.split('\n')
        if len(lines) <= max_lines:
            return content
        truncated = lines[:max_lines]
        truncated.append(f"... ({len(lines) - max_lines} linhas truncadas)")
        return '\n'.join(truncated)
    
    def _truncate_payload(self, payload: dict, max_lines: int = 10) -> dict:
        """Truncate all string fields in a tool payload to reduce verbosity."""
        if not payload:
            return payload
        
        truncated = payload.copy()
        # Truncate content field
        if isinstance(truncated.get('content'), str):
            truncated['content'] = self._truncate_tool_result(truncated['content'], max_lines)
        # Truncate error field
        if isinstance(truncated.get('error'), str):
            truncated['error'] = self._truncate_tool_result(truncated['error'], max_lines)
        # Truncate string values in data field
        if isinstance(truncated.get('data'), dict):
            data = truncated['data'].copy()
            for key, value in data.items():
                if isinstance(value, str):
                    data[key] = self._truncate_tool_result(value, max_lines)
            truncated['data'] = data
        return truncated

    def resolve_agent_response(self, agent: str, response: str | None, silent: bool = False) -> str | None:
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
            tool_payload = self._truncate_payload(tool_result.to_model_payload())
            
            tool_history.append(
                f"Sua resposta anterior:\n{current_response.strip()}\n\n"
                f"Resultado da ferramenta:\n{tool_payload}"
            )

            visible_text = strip_tool_block(raw_response or "")
            if visible_text:
                self.print_response(agent, visible_text)
                self.persist_message(agent, visible_text)

            followup_handoff = (
                "Histórico de ferramentas desta rodada:\n\n"
                + "\n\n---\n\n".join(tool_history)
                + "\n\nContinue a partir daqui. Se precisar de outra ferramenta, emita novo bloco ```tool```."
            )

            current_response = self._call_agent(
                agent,
                handoff=followup_handoff,
                primary=False,
                protocol_mode="tool_loop",
                silent=silent,
            )

        return "Falha: limite de execuções de ferramenta atingido."

    def read_user_input(self, prompt, timeout: int) -> str | None:
        """Read user input with optional idle timeout.

        If idle timeout is enabled and expires, emit a '*idle* (Xd s sem activity)'
        line and return None to signal that no input was received.
        """
        if timeout and timeout > 0:
            value = self._read_user_input_with_timeout(prompt, timeout)
            if value is None:
                self.renderer.show_system(f"*idle* ({timeout}s sem activity)")
                return None
            return value
        # Fallback: blocking read
        try:
            return input(prompt)
        except EOFError:
            # When timeout=0, treat EOF as no input available
            if timeout == 0:
                return None
            raise
        except KeyboardInterrupt:
            print()
            raise

    @staticmethod
    def _read_user_input_with_timeout(prompt: str, timeout: int):
        # Uses a thread to call input() so we can timeout in the main thread.
        import queue
        q = queue.Queue()

        def _reader():
            try:
                val = input(prompt)
                q.put(val)
            except Exception:
                q.put(None)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None

    def handle_command(self, user_input):
        command = user_input.strip()

        if command == CMD_HELP:
            self.renderer.show_system(build_help(self.active_agents))
            return True

        if command == CMD_CONTEXT:
            self.context_manager.show()
            return True

        if command == CMD_CONTEXT_EDIT:
            self.context_manager.edit()
            return True

        return False

    def read_from_editor(self):
        """Abre $EDITOR num arquivo temporário e retorna o conteúdo digitado."""
        import shlex
        import shutil
        editor_env = os.environ.get("EDITOR", "")
        if editor_env:
            editor_parts = shlex.split(editor_env)
        else:
            fallbacks = ["nano", "vim", "vi"]
            editor_parts = next(
                ([e] for e in fallbacks if shutil.which(e)), None
            )
            if not editor_parts:
                self.renderer.show_error("\nNenhum editor encontrado. Defina $EDITOR ou instale nano/vim.\n")
                return None
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run([*editor_parts, tmp_path], check=True)
            content = Path(tmp_path).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            self.renderer.show_error(f"\nEditor não encontrado: {editor_parts[0]}\n")
            return None
        except subprocess.CalledProcessError as exc:
            self.renderer.show_error(f"\nEditor encerrou com erro (código {exc.returncode}).\n")
            return None
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return content or None

    def read_from_file(self, path_str):
        """Lê o conteúdo de um arquivo e retorna como string."""
        path = Path(path_str).expanduser()
        if not path.exists():
            self.renderer.show_error(f"\nArquivo não encontrado: {path}\n")
            return None
        content = path.read_text(encoding="utf-8").strip()
        return content or None

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
            self.active_agents = ["*"]
        return self.active_agents[0], user_input, False

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

    def call_agent(self, agent, **options):
        silent = options.get("protocol_mode") == "task_execution"
        _logger.info("[DISPATCH] sending to agent=%s, handoff_only=%s", agent, options.get("handoff_only", False))
        try:
            response = self._call_agent(agent, silent=silent, **options)
            if response is None:
                self._record_failure(agent)
                return None
            result = self.resolve_agent_response(agent, response, silent=silent)
            if result is None:
                self._record_failure(agent)
            return result
        except Exception:
            self._record_failure(agent)
            raise

    def _call_agent(self, agent, is_first_speaker=False, handoff=None, primary=True, protocol_mode="standard", handoff_only=False, silent=False):
        self.session_call_index += 1
        history = [] if handoff_only else self.history
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
            )
            self.agent_client.log_prompt_metrics(
                agent, metrics,
                session_id=self.session_state["session_id"],
                round_index=self.round_index,
                session_call_index=self.session_call_index,
                history_window=self.prompt_builder.history_window,
                protocol_mode=protocol_mode,
            )
        else:
            prompt = self.prompt_builder.build(
                agent, history, is_first_speaker, handoff,
                primary=primary, shared_state=self.shared_state,
                handoff_only=handoff_only,
            )
        return self.agent_client.call(agent, prompt, silent=silent)

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

    def parse_handoff_payload(self, payload):
        if not payload:
            return None
        match = self.HANDOFF_PAYLOAD_PATTERN.match(payload.strip())
        if not match:
            _logger.warning(f"[HANDOFF] Payload did not match regex: {payload!r}")
            return None

        task, context, expected = (group.strip() if group else None for group in match.groups())
        if not task:
            _logger.warning(f"[HANDOFF] Missing required field 'task' - got task={task!r}, context={context!r}, expected={expected!r}")
            return None

        return {
            "task": task,
            "context": context,
            "expected": expected,
        }

    def parse_response(self, response):
        """Extrai marcadores de controle e retorna (clean, route_target, handoff, extend, needs_human_input)."""
        if response is None:
            return None, None, None, False, False

        route_target, handoff = None, None

        if STATE_UPDATE_START in response:
            for state_match in self.STATE_UPDATE_PATTERN.finditer(response):
                self._apply_state_update(state_match.group(1))
            response = self.STATE_UPDATE_PATTERN.sub("", response).strip()

        if ROUTE_PREFIX in response:
            match = self.ROUTE_PATTERN.search(response)
            if match:
                raw_payload = self._strip_payload_residual(match.group(2))
                parsed_handoff = self.parse_handoff_payload(raw_payload)
                _logger.info("[ROUTE] match=%s, target=%s", match.group(0)[:100], match.group(1) if match.group(1) else None)
                if parsed_handoff:
                    route_target = match.group(1)
                    handoff = parsed_handoff
                else:
                    _logger.warning("[ROUTE] handoff parse failed for target=%s, payload: %r", match.group(1), raw_payload)
                response = self.ROUTE_PATTERN.sub("", response, count=1).strip() or "..."

        extend = response.rstrip().endswith(EXTEND_MARKER)
        if extend:
            response = response.rstrip()[: -len(EXTEND_MARKER)].rstrip()

        needs_human_input = NEEDS_INPUT_MARKER in response
        if needs_human_input:
            response = response.replace(NEEDS_INPUT_MARKER, "").strip()

        return response, route_target, handoff, extend, needs_human_input

    def _call_agent_for_parallel(self, agent, handoff, protocol_mode, staging_root, index):
        """Executa call_agent e retorna tupla (agent, response, route_target, handoff, extend, needs_input)."""
        from .runtime.tools.files import set_staging_root
        
        set_staging_root(staging_root / str(index))
        try:
            raw = self.call_agent(agent, handoff=handoff, primary=False, protocol_mode=protocol_mode)
            response, route_target, handoff, extend, needs_input = self.parse_response(raw)
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
        with self._lock:
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

        try:
            while True:
                user = self.read_user_input(f"{self.user_name}: ", timeout=0)
                
                # Handle case where no input is available (timeout=0 and EOF)
                if user is None:
                    # EOF reached in non-interactive mode, exit cleanly
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

                first_agent, message, explicit = self.parse_routing(user)
                if first_agent is None:
                    continue
                if not message or not message.strip():
                    self.renderer.show_warning(MSG_EMPTY_INPUT.format(first_agent))
                    continue

                other_agents = [n for n in self.active_agents if n != first_agent]

                self.round_index += 1
                self.summary_agent_preference = first_agent
                self.persist_message(USER_ROLE, message)

                # Primeira fala: detecta roteamento ou debate estendido
                response = self.call_agent(first_agent, is_first_speaker=True, protocol_mode="standard")
                response, route_target, handoff, extend, needs_human_input = self.parse_response(response)

                if needs_human_input:
                    self.renderer.show_message(first_agent, response)
                    self.renderer.show_system("\nO agente precisa de input humano...\n")
                    user_input = self.read_user_input("Sua resposta: ", self.idle_timeout_seconds)
                    if user_input:
                        handoff = handoff or {}
                        handoff["human_input"] = user_input
                        response = self.call_agent(
                            first_agent,
                            handoff=handoff,
                            primary=False,
                            protocol_mode="handoff",
                        )
                        response, route_target, handoff, extend, _ = self.parse_response(response)
                self.print_response(first_agent, response)
                if response is not None:
                    self.persist_message(first_agent, response)

                # Um handoff emitido pela primeira resposta sempre tem prioridade,
                # inclusive quando a rodada começou com /claude ou /codex.
                if route_target and handoff:
                    self.renderer.show_handoff(
                        first_agent,
                        route_target,
                        task=handoff["task"],
                    )
                    # Handoff v1: agente secundário recebe apenas o payload delegado
                    secondary_response = self.call_agent(
                        route_target,
                        handoff=handoff,
                        handoff_only=True,
                        primary=False,
                        protocol_mode="handoff",
                    )
                    secondary_response, _, _, _, _ = self.parse_response(secondary_response)
                    self.print_response(route_target, secondary_response)
                    if secondary_response is not None:
                        self.persist_message(route_target, secondary_response)

                    # Integrador: agente primário sintetiza com a resposta do secundário
                    if secondary_response:
                        synthesis_handoff = HANDOFF_SYNTHESIS_MSG.format(
                            agent=route_target.upper(),
                            task=handoff["task"],
                            response=secondary_response,
                        )
                        final_response = self.call_agent(
                            first_agent,
                            handoff=synthesis_handoff,
                            primary=False,
                            protocol_mode="handoff",
                        )
                        final_response, _, _, _, _ = self.parse_response(final_response)
                        self.print_response(first_agent, final_response)
                        if final_response is not None:
                            self.persist_message(first_agent, final_response)
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
                                # Levar fluxo de input humano para o primeiro agente que requer
                                needing = next((a for a in results if a[-1]), None)
                                if needing:
                                    _, _, needing_route_target, needing_handoff, _, _ = needing
                                    # Pergunta ao humano
                                    user_input = input("Sua resposta: ").strip()
                                    if user_input:
                                        current_agent = needing[0]
                                        handoff_payload = needing_handoff or {}
                                        if isinstance(handoff_payload, dict):
                                            handoff_payload["human_input"] = user_input
                                        else:
                                            handoff_payload = {"human_input": user_input}
                                        final_response = self.call_agent(
                                            current_agent,
                                            handoff=handoff_payload,
                                            primary=False,
                                            protocol_mode="handoff",
                                        )
                                        final_response, _, _, _, _ = self.parse_response(final_response)
                                        self.print_response(current_agent, final_response)
                                        if final_response is not None:
                                            self.persist_message(current_agent, final_response)
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
                            response, route_target, handoff, _, _ = self.parse_response(response)
                            self.print_response(agent, response)
                            if response is not None:
                                self.persist_message(agent, response)
                            if route_target and index + 1 < len(remaining):
                                remaining[index + 1] = route_target
                            if route_target:
                                next_handoff = handoff

                self._maybe_auto_summarize(preferred_agent=first_agent)
        except KeyboardInterrupt:
            self.renderer.show_system(MSG_SHUTDOWN)
        finally:
            self.shutdown()
