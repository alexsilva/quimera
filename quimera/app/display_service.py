"""Serviço de display — encapsula renderer, thread-safety e mensagens diferidas."""
from __future__ import annotations
import threading
import re
from contextlib import nullcontext


_TASK_ID_RE = re.compile(r'\[task (\d+)\]')

_TERMINAL_TASK_KEYWORDS = (
    "concluída",
    "falhou",
    "cancelad",
)

_RETRY_TASK_KEYWORDS = (
    "bloqueada",
    "sem resposta",
    "erro:",
    "requeue",
)


class DisplayService:
    """Encapsula toda interação com o renderer, incluindo thread-safety e mensagens diferidas."""

    _SUPPRESSED_TASK_STATUS_FRAGMENTS = (
        ": iniciando",
        ": aguardando review de outro agente",
        ": revisando task",
        ": revisando execução de ",
        ": review rejeitado, aguardando outro agente",
    )

    def __init__(
        self,
        renderer=None,
        input_status_getter=None,
        redisplay_prompt=None,
        output_lock=None,
        prompt_owner_thread_id_getter=None,
        run_above_active_prompt=None,
        deferred_messages_getter=None,
        max_deferred_messages_getter=None,
    ):
        self.renderer = renderer
        self.input_status_getter = input_status_getter or (lambda: "idle")
        self.redisplay_prompt = redisplay_prompt or (lambda clear_first=True: None)
        self.output_lock = output_lock or nullcontext()
        self.prompt_owner_thread_id_getter = prompt_owner_thread_id_getter or (lambda: None)
        self.run_above_active_prompt = run_above_active_prompt
        self._deferred_system_messages: list[tuple[str, str]] = []
        self._deferred_messages_getter = deferred_messages_getter
        self._max_deferred_messages_getter = max_deferred_messages_getter

    def _get_renderer(self):
        """Resolve o renderer atual, com suporte a injeção dinâmica legada."""
        if callable(self.renderer) and not hasattr(self.renderer, "show_system"):
            return self.renderer()
        return self.renderer

    def _get_output_lock(self):
        """Resolve o lock de output atual."""
        if callable(self.output_lock) and not hasattr(self.output_lock, "__enter__"):
            return self.output_lock() or nullcontext()
        return self.output_lock

    def _get_deferred_messages(self) -> list:
        """Resolve a fila de mensagens diferidas."""
        if callable(self._deferred_messages_getter):
            return self._deferred_messages_getter()
        return self._deferred_system_messages

    def _get_max_deferred_messages(self) -> int:
        """Resolve o limite atual da fila diferida."""
        if callable(self._max_deferred_messages_getter):
            return self._max_deferred_messages_getter()
        return 20

    def _should_suppress_active_prompt_message(self, message: str) -> bool:
        """Suprime status transitório de task para evitar churn no prompt."""
        if not self._is_prompt_active():
            return False
        if "\n" in message or not message.startswith("[task "):
            return False
        return any(fragment in message for fragment in self._SUPPRESSED_TASK_STATUS_FRAGMENTS)

    def _should_defer_active_prompt_message(self, message: str) -> bool:
        """Adia mensagens de task enquanto o input TTY estiver ativo."""
        return (
            self._is_prompt_active()
            and message.startswith("[task ")
            and "\n" not in message
            and ": concluída" not in message
            and ": review concluído" not in message
        )

    @staticmethod
    def _is_terminal_task_feedback(message: str) -> bool:
        """Identifica feedback final curto de task que merece aparecer na hora."""
        return (
            message.startswith("[task ")
            and "\n" not in message
            and " concluída" in message
        )

    @staticmethod
    def _extract_task_id(message: str) -> int | None:
        """Extrai o ID numérico de uma task no formato [task N]."""
        m = _TASK_ID_RE.search(message)
        return int(m.group(1)) if m else None

    @staticmethod
    def _is_terminal_task_message(message: str) -> bool:
        """Verifica se a mensagem contém indicador terminal de task."""
        first_line = message.split("\n", 1)[0]
        return any(kw in first_line for kw in _TERMINAL_TASK_KEYWORDS)

    @staticmethod
    def _format_task_summary(task_id: int, message: str, retry_count: int = 0) -> str:
        """Formata mensagem terminal de task como linha compacta."""
        task_tag = f"[task {task_id}]"
        body = message
        if message.startswith(task_tag):
            body = message[len(task_tag):].lstrip()

        suffix = f" (após {retry_count} tentativas)" if retry_count > 0 else ""
        return f"⚙ [task {task_id}] {body}{suffix}"

    @staticmethod
    def _compact_deferred(deferred: list) -> list:
        """Remove mensagens transitórias e compacta conclusões de task no lote."""
        terminal_tasks: set[int] = set()
        retry_counts: dict[int, int] = {}
        last_terminal_idx: dict[int, int] = {}

        for idx, item in enumerate(deferred):
            msg = item[1] if isinstance(item, tuple) and len(item) == 2 else str(item)
            task_id = DisplayService._extract_task_id(msg)
            if task_id is not None:
                if DisplayService._is_terminal_task_message(msg):
                    terminal_tasks.add(task_id)
                    last_terminal_idx[task_id] = idx
                elif any(kw in msg for kw in _RETRY_TASK_KEYWORDS):
                    retry_counts[task_id] = retry_counts.get(task_id, 0) + 1

        if not terminal_tasks:
            return DisplayService._dedup_without_terminal(deferred)

        result: list = []

        for idx, item in enumerate(deferred):
            msg = item[1] if isinstance(item, tuple) and len(item) == 2 else str(item)
            task_id = DisplayService._extract_task_id(msg)

            if task_id is not None and task_id in terminal_tasks:
                if not DisplayService._is_terminal_task_message(msg):
                    continue
                if idx != last_terminal_idx.get(task_id):
                    continue

                retry_count = retry_counts.get(task_id, 0)
                formatted = DisplayService._format_task_summary(
                    task_id, msg, retry_count,
                )
                if isinstance(item, tuple) and len(item) == 2:
                    item = (item[0], formatted)
                else:
                    item = formatted

            result.append(item)

        return result

    @staticmethod
    def _dedup_without_terminal(deferred: list) -> list:
        """Dedup messages even when no terminal message exists (task still running)."""
        last_msg_by_task: dict[int, tuple] = {}
        for item in deferred:
            msg = item[1] if isinstance(item, tuple) and len(item) == 2 else str(item)
            task_id = DisplayService._extract_task_id(msg)
            if task_id is not None:
                last_msg_by_task[task_id] = item

        return list(last_msg_by_task.values()) if last_msg_by_task else list(deferred)

    def _is_input_reading(self) -> bool:
        """Normaliza leitura de estado de prompt ativo (bool moderno ou string legada)."""
        status = self.input_status_getter()
        if isinstance(status, bool):
            return status
        return status == "reading"

    def _is_prompt_active(self) -> bool:
        """Retorna se há um prompt interativo ativo no momento."""
        return self._is_input_reading()

    def _is_prompt_owner_thread(self) -> bool:
        """Retorna se a thread atual é a dona do prompt interativo."""
        current_thread_id = self.prompt_owner_thread_id_getter()
        return current_thread_id is not None and current_thread_id == threading.get_ident()

    def _is_foreign_prompt_thread(self) -> bool:
        """Retorna se outra thread é a dona do prompt interativo."""
        current_thread_id = self.prompt_owner_thread_id_getter()
        return current_thread_id is not None and current_thread_id != threading.get_ident()

    def _enqueue_deferred_message(self, message: str, level: str = "system") -> bool:
        """Enfileira mensagem diferida preservando o tipo visual."""
        deferred_list = self._get_deferred_messages()
        max_deferred = self._get_max_deferred_messages()
        if len(deferred_list) >= max_deferred:
            overflow = len(deferred_list) - max_deferred + 1
            del deferred_list[:overflow]
        deferred_list.append((level, message))
        renderer = self._get_renderer()
        if renderer is not None:
            audit_logger = getattr(renderer, "_audit_logger", None)
            if audit_logger is not None:
                payload: dict = dict(message=message[:120], level=level)
                task_id = self._extract_task_id(message)
                if task_id is not None:
                    payload["task_id"] = task_id
                audit_logger.log_event("deferred_enqueue", **payload)
        return True

    @staticmethod
    def _render_message(renderer, level: str, message: str) -> None:
        """Renderiza uma mensagem sem mexer diretamente no prompt."""
        if level == "neutral" and hasattr(renderer, "show_system_neutral"):
            renderer.show_system_neutral(message)
        elif level == "warning" and hasattr(renderer, "show_warning"):
            renderer.show_warning(message)
        elif level == "error" and hasattr(renderer, "show_error"):
            renderer.show_error(message)
        else:
            renderer.show_system(message)

    @staticmethod
    def _flush_renderer(renderer, *, prefer_quick: bool = False) -> None:
        """Realiza flush do renderer, com modo rápido opcional para evitar travar o prompt."""
        if prefer_quick:
            flush_quick = getattr(renderer, "flush_quick", None)
            if callable(flush_quick):
                flush_quick()
                return
        flush = getattr(renderer, "flush", None)
        if callable(flush):
            flush()

    def _show_above_active_prompt(self, message: str, *, level: str) -> bool:
        """Tenta publicar a mensagem acima do prompt ativo via InputGate."""
        run_in_terminal_message = self.run_above_active_prompt
        if not callable(run_in_terminal_message):
            return False
        renderer = self._get_renderer()
        if renderer is None:
            return False

        def _render_callback() -> None:
            with self._get_output_lock():
                self._render_message(renderer, level, message)
                self._flush_renderer(renderer)

        return bool(run_in_terminal_message(_render_callback))

    def show_system_message(self, message: str) -> None:
        """Exibe system message."""
        renderer = self._get_renderer()
        if renderer is None:
            return
        if self._should_suppress_active_prompt_message(message):
            return
        if self._should_defer_active_prompt_message(message):
            if self._enqueue_deferred_message(message, level="system"):
                return
        if self._is_prompt_active() and self._is_foreign_prompt_thread():
            if self._show_above_active_prompt(message, level="system"):
                return
            if self._enqueue_deferred_message(message, level="system"):
                return
        with self._get_output_lock():
            current_thread_id = self.prompt_owner_thread_id_getter()
            is_owning = current_thread_id is None or current_thread_id == threading.get_ident()
            renderer.show_system(message)
            self._flush_renderer(renderer, prefer_quick=is_owning)
            if is_owning:
                self.redisplay_prompt(clear_first=False)

    def show_muted_message(self, message: str) -> None:
        """Exibe mensagem em estilo neutro (dim) via writer thread do renderer."""
        renderer = self._get_renderer()
        if renderer is None:
            return
        if self._is_prompt_active() and self._is_foreign_prompt_thread():
            if self._show_above_active_prompt(message, level="neutral"):
                return
            if self._enqueue_deferred_message(message, level="neutral"):
                return
        with self._get_output_lock():
            current_thread_id = self.prompt_owner_thread_id_getter()
            is_owning = current_thread_id is not None and current_thread_id == threading.get_ident()
            show_system_neutral = getattr(renderer, "show_system_neutral", None)
            if callable(show_system_neutral):
                show_system_neutral(message)
            else:
                renderer.show_system(message)
            self._flush_renderer(renderer, prefer_quick=is_owning)
            if is_owning:
                self.redisplay_prompt(clear_first=False)

    def show_warning_message(self, message: str) -> None:
        """Exibe warning de forma compatível com prompt ativo e background threads."""
        renderer = self._get_renderer()
        if renderer is None:
            return
        if self._is_prompt_active() and self._is_foreign_prompt_thread():
            if self._show_above_active_prompt(message, level="warning"):
                return
            if self._enqueue_deferred_message(message, level="warning"):
                return
        with self._get_output_lock():
            current_thread_id = self.prompt_owner_thread_id_getter()
            is_owning = current_thread_id is not None and current_thread_id == threading.get_ident()
            show_warning = getattr(renderer, "show_warning", None)
            if callable(show_warning):
                show_warning(message)
            else:
                renderer.show_system(message)
            self._flush_renderer(renderer, prefer_quick=is_owning)
            if is_owning:
                self.redisplay_prompt(clear_first=False)

    def show_error_message(self, message: str) -> None:
        """Exibe error de forma compatível com prompt ativo e background threads."""
        renderer = self._get_renderer()
        if renderer is None:
            return
        if self._is_prompt_active() and self._is_foreign_prompt_thread():
            if self._show_above_active_prompt(message, level="error"):
                return
            if self._enqueue_deferred_message(message, level="error"):
                return
        with self._get_output_lock():
            current_thread_id = self.prompt_owner_thread_id_getter()
            is_owning = current_thread_id is not None and current_thread_id == threading.get_ident()
            show_error = getattr(renderer, "show_error", None)
            if callable(show_error):
                show_error(message)
            else:
                renderer.show_system(message)
            self._flush_renderer(renderer, prefer_quick=is_owning)
            if is_owning:
                self.redisplay_prompt(clear_first=False)

    def show_prompt_preview(self, agent: str, preview: str) -> None:
        """Exibe preview de prompt via renderer."""
        renderer = self._get_renderer()
        if renderer is None:
            return
        show = getattr(renderer, "show_prompt_preview", None)
        if callable(show):
            show(agent, preview)

    def show_system(self, message: str) -> None:
        """Exibe mensagem diretamente via renderer.show_system (sem thread-safety)."""
        renderer = self._get_renderer()
        if renderer is None:
            return
        renderer.show_system(message)

    def show_warning(self, message: str) -> None:
        """Exibe warning diretamente via renderer.show_warning (sem thread-safety)."""
        renderer = self._get_renderer()
        if renderer is None:
            return
        renderer.show_warning(message)

    def flush_deferred_messages(self) -> None:
        """Exibe mensagens de sistema adiadas quando o prompt deixa de estar ativo."""
        deferred = self._get_deferred_messages()
        if not deferred:
            return
        renderer = self._get_renderer()
        if renderer is None:
            deferred.clear()
            return
        audit_logger = getattr(renderer, "_audit_logger", None)
        if audit_logger is not None:
            audit_logger.log_event(
                "deferred_flush",
                count=len(deferred),
                previews=[msg[:80] for _, msg in deferred],
            )
        compacted = self._compact_deferred(deferred)
        with self._get_output_lock():
            for item in compacted:
                if isinstance(item, tuple) and len(item) == 2:
                    level, message = item
                else:
                    level, message = "system", item
                self._render_message(renderer, level, message)
            self._flush_renderer(renderer)
            deferred.clear()
