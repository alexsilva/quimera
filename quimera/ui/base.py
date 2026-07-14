"""Contrato base de renderers com defaults no-op/fallback.

`RendererBase` materializa o contrato completo que os consumidores usam
(`app/display_service.py`, `app/dispatch.py`, `app/chat_round.py`, ...),
eliminando a necessidade de capability-sniffing via ``getattr``. Regras:

- Exibições opcionais delegam para :meth:`show_system` (único método que a
  subclasse é obrigada a implementar).
- Capacidades de infraestrutura (``flush``, ``set_summarizing``, ...) são
  no-op.
- Capacidades booleanas são atributos de classe (``supports_agent_feed``).
- ``clear_screen`` fica fora do contrato por ora: o fallback ANSI vive em
  ``core_facade.clear_terminal_screen`` e será movido para cá na fase 4 do
  PLAN_RENDERER_PROTOCOL.md.
"""
from __future__ import annotations

from quimera.ui.messages import (
    FAILOVER_DEFAULT_MESSAGE,
    format_failover_message,
    format_retry_message,
)


class RendererBase:
    """Base de renderers: no-ops e fallbacks textuais para o contrato opcional."""

    supports_agent_feed = False
    #: True quando o renderer tem canal estruturado para atividade de agente
    #: (retry/failover); False manda os chamadores usarem o caminho textual
    #: prompt-aware (show_*_message) em vez de notify_*.
    supports_structured_agent_activity = False

    # ------------------------------------------------------------------
    # Núcleo obrigatório
    # ------------------------------------------------------------------

    def show_system(self, message):
        raise NotImplementedError("Renderer deve implementar show_system()")

    def show_warning(self, message):
        self.show_system(message)

    def show_error(self, message, **metadata):
        self.show_system(message)

    # ------------------------------------------------------------------
    # Exibições opcionais — fallback textual via show_system
    # ------------------------------------------------------------------

    def show_banner(self, message):
        self.show_system(message)

    def show_system_neutral(self, message):
        self.show_system(message)

    def show_approval(self, message):
        self.show_system(message)

    def show_feed(self, message, agent=None, muted=False):
        self.show_system(message)

    def notify_agent_retry(self, agent, *, reason, attempt, limit, detail=""):
        self.show_warning(format_retry_message(reason, attempt, limit, detail))

    def notify_agent_failover(self, agent, *, target, message=FAILOVER_DEFAULT_MESSAGE):
        self.show_system(format_failover_message(agent, target, message))

    # ------------------------------------------------------------------
    # Fluxo de agentes — no-op quando o renderer não tem canal estruturado
    # ------------------------------------------------------------------

    def show_message(self, agent, content, render_mode="auto"):
        self.show_system(content)

    def show_no_response(self, agent):
        return None

    def show_delegation(self, from_agent, to_agent, task=None, *, delegation_id=None, chain=None):
        return None

    def show_prompt_preview(self, agent, preview):
        return None

    def update_agent_transient(self, agent, message):
        return None

    def commit_agent_stream(self, agent, render_mode="auto"):
        return False

    # ------------------------------------------------------------------
    # Infraestrutura — no-op
    # ------------------------------------------------------------------

    def flush(self, timeout=5.0):
        return None

    def flush_quick(self, timeout=0.15):
        # Renderer sem flush rápido dedicado faz o flush normal (mesma
        # semântica do fallback getattr histórico dos chamadores).
        self.flush()
        return True

    def signal_restore_history(self):
        return None

    def set_summarizing(self, active):
        return None

    def set_prompt_integration(self, is_active_fn, run_above_fn):
        return None

    def log_debug_event(self, event, **payload):
        return None

    # ------------------------------------------------------------------
    # Temas
    # ------------------------------------------------------------------

    @property
    def theme_name(self):
        return ""

    def cycle_theme(self):
        return None
