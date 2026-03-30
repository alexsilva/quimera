import json
import locale
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .ui import TerminalRenderer
from .context import ContextManager
from .storage import SessionStorage
from .agents import AgentClient
from .session_summary import SessionSummarizer, build_chain_summarizer
from .prompt import PromptBuilder
from .workspace import Workspace
from .config import ConfigManager
from .constants import (
    EXTEND_MARKER,
    ROUTE_PREFIX,
    STATE_UPDATE_START, STATE_UPDATE_END,
    CMD_EXIT, CMD_HELP, CMD_CONTEXT, CMD_CONTEXT_EDIT, CMD_EDIT, CMD_FILE_PREFIX,
    PREFIX_CLAUDE, PREFIX_CODEX,
    AGENT_CLAUDE, AGENT_CODEX, DEFAULT_FIRST_AGENT, AGENT_SEQUENCE,
    USER_ROLE, INPUT_PROMPT,
    MSG_CHAT_STARTED, MSG_SESSION_LOG, MSG_SESSION_STATUS, MSG_HELP, MSG_MIGRATION,
    MSG_MEMORY_SAVING, MSG_MEMORY_FAILED, MSG_SHUTDOWN,
    MSG_DOUBLE_PREFIX, MSG_EMPTY_INPUT,
    HANDOFF_SYNTHESIS_MSG,
)


class QuimeraApp:
    """Orquestra comandos locais, roteamento entre agentes e ciclo da sessão."""
    ROUTE_PATTERN = re.compile(r"(?m)^\[ROUTE:(claude|codex)\]\s*(.+?)\s*$")
    HANDOFF_PAYLOAD_PATTERN = re.compile(
        r"^\s*task:\s*(.*?)\s*\|\s*context:\s*(.*?)\s*\|\s*expected:\s*(.*?)\s*$",
        re.IGNORECASE | re.DOTALL,
    )
    STATE_UPDATE_PATTERN = re.compile(
        r"\[STATE_UPDATE\](.*?)\[/STATE_UPDATE\]", re.DOTALL
    )

    @staticmethod
    def _format_yes_no(value):
        return "sim" if value else "não"

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

    def __init__(self, cwd: Path, debug: bool = False, history_window: int | None = None):
        self.renderer = TerminalRenderer()
        self.config = ConfigManager()
        self.user_name = self.config.user_name
        workspace = Workspace(cwd)

        migrated = workspace.migrate_from_legacy(cwd)
        for item in migrated:
            self.renderer.show_system(MSG_MIGRATION.format(item))

        self.context_manager = ContextManager(
            workspace.context_persistent,
            workspace.context_session,
            self.renderer,
        )
        self.storage = SessionStorage(workspace.logs_dir, self.renderer)
        session_id = self.storage.get_history_file().stem
        metrics_file = workspace.metrics_dir / f"{session_id}.jsonl" if debug else None
        self.agent_client = AgentClient(self.renderer, metrics_file=metrics_file)
        self.session_summarizer = SessionSummarizer(
            self.renderer,
            summarizer_call=build_chain_summarizer(self.agent_client, [AGENT_CLAUDE, AGENT_CODEX]),
        )
        self.summary_agent_preference = DEFAULT_FIRST_AGENT
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

    def _input_encoding_candidates(self):
        stdin_encoding = getattr(sys.stdin, "encoding", None)
        device_encoding = None
        try:
            if hasattr(sys.stdin, "fileno"):
                device_encoding = os.device_encoding(sys.stdin.fileno())
        except (OSError, ValueError):
            device_encoding = None

        return self._unique_encodings(
            stdin_encoding,
            device_encoding,
            locale.getpreferredencoding(False),
            "utf-8",
            "cp1252",
            "latin-1",
        )

    def _decode_stdin_bytes(self, raw_line):
        payload = raw_line.rstrip(b"\r\n")
        for encoding in self._input_encoding_candidates():
            try:
                return payload.decode(encoding)
            except UnicodeDecodeError:
                continue

        fallback = self._input_encoding_candidates()[0] if self._input_encoding_candidates() else "utf-8"
        return payload.decode(fallback, errors="replace")

    def read_user_input(self):
        prompt = f"{self.user_name}: "
        stdout = getattr(sys, "stdout", None)
        if stdout is not None:
            stdout.write(prompt)
            stdout.flush()

        stdin_buffer = getattr(sys.stdin, "buffer", None)
        if stdin_buffer is not None:
            raw_line = stdin_buffer.readline()
            if raw_line == b"":
                raise EOFError
            return self._decode_stdin_bytes(raw_line)

        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")

    def handle_command(self, user_input):
        command = user_input.strip()

        if command == CMD_HELP:
            self.renderer.show_system(MSG_HELP)
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
        import shlex, shutil
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

        for prefix, agent in AGENT_SEQUENCE:
            if lowered == prefix:
                return agent, "", True
            if lowered.startswith(f"{prefix} "):
                message = stripped[len(prefix):].lstrip()
                other_prefix = PREFIX_CLAUDE if prefix == PREFIX_CODEX else PREFIX_CODEX
                lowered_message = message.lower()
                if lowered_message == other_prefix or lowered_message.startswith(f"{other_prefix} "):
                    self.renderer.show_warning(MSG_DOUBLE_PREFIX)
                    return None, None, False
                return agent, message, True

        return DEFAULT_FIRST_AGENT, user_input, False

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

    def call_agent(self, agent, is_first_speaker=False, handoff=None, primary=True, protocol_mode="standard", handoff_only=False):
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
        return self.agent_client.call(agent, prompt)

    def parse_handoff_payload(self, payload):
        if not payload:
            return None
        match = self.HANDOFF_PAYLOAD_PATTERN.match(payload.strip())
        if not match:
            return None

        task, context, expected = (group.strip() for group in match.groups())
        if not task or not context or not expected:
            return None

        return {
            "task": task,
            "context": context,
            "expected": expected,
        }

    def parse_response(self, response):
        """Extrai marcadores de controle e retorna (clean, route_target, handoff, extend)."""
        if response is None:
            return None, None, None, False

        route_target, handoff = None, None

        if STATE_UPDATE_START in response:
            for state_match in self.STATE_UPDATE_PATTERN.finditer(response):
                self._apply_state_update(state_match.group(1))
            response = self.STATE_UPDATE_PATTERN.sub("", response).strip()

        if ROUTE_PREFIX in response:
            match = self.ROUTE_PATTERN.search(response)
            if match:
                parsed_handoff = self.parse_handoff_payload(match.group(2).strip())
                if parsed_handoff:
                    route_target = match.group(1)
                    handoff = parsed_handoff
                response = self.ROUTE_PATTERN.sub("", response, count=1).strip()

        extend = response.rstrip().endswith(EXTEND_MARKER)
        if extend:
            response = response.rstrip()[: -len(EXTEND_MARKER)].rstrip()

        return response, route_target, handoff, extend

    def print_response(self, agent, response):
        if response is not None:
            self.renderer.show_message(agent, response)
        else:
            self.renderer.show_no_response(agent)

    def persist_message(self, role, content):
        """Persiste uma mensagem no histórico em memória, log e snapshot JSON."""
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
            DEFAULT_FIRST_AGENT,
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
        if not self.history:
            return

        self.renderer.show_system(MSG_MEMORY_SAVING)

        summary = self.session_summarizer.summarize(
            self.history,
            existing_summary=self.context_manager.load_session_summary(),
            preferred_agent=getattr(self, "summary_agent_preference", DEFAULT_FIRST_AGENT),
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
                user = self.read_user_input()

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
                if not message.strip():
                    self.renderer.show_warning(MSG_EMPTY_INPUT.format(first_agent))
                    continue

                second_agent = AGENT_CODEX if first_agent == AGENT_CLAUDE else AGENT_CLAUDE

                self.round_index += 1
                self.summary_agent_preference = first_agent
                self.persist_message(USER_ROLE, message)

                # Primeira fala: detecta roteamento ou debate estendido
                response = self.call_agent(first_agent, is_first_speaker=True, protocol_mode="standard")
                response, route_target, handoff, extend = self.parse_response(response)
                self.print_response(first_agent, response)
                if response is not None:
                    self.persist_message(first_agent, response)

                # Um handoff emitido pela primeira resposta sempre tem prioridade,
                # inclusive quando a rodada começou com /claude ou /codex.
                if route_target:
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
                    secondary_response, _, _, _ = self.parse_response(secondary_response)
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
                        final_response, _, _, _ = self.parse_response(final_response)
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
                        remaining = [second_agent, first_agent, second_agent]
                    else:
                        remaining = [second_agent]

                    next_handoff = None
                    for index, agent in enumerate(remaining):
                        response = self.call_agent(agent, handoff=next_handoff, primary=False, protocol_mode=protocol_mode)
                        next_handoff = None
                        response, route_target, handoff, _ = self.parse_response(response)
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
