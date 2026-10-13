"""Comando humano ``/workflow``: lista, mostra, pausa, retoma e cancela workflows."""
from __future__ import annotations

from collections.abc import Callable

from .coordinator import WorkflowCoordinator
from .models import OPEN_WORKFLOW_STATES, WorkflowError
from .presentation import render_text

USAGE = "Uso: /workflow [list|show <id>|pause <id>|resume <id>|cancel <id>]"

class WorkflowCommandHandler:
    """Traduz ``/workflow`` em chamadas ao coordenador; o humano é o dono de qualquer workflow."""

    def __init__(
        self,
        get_coordinator: Callable[[], WorkflowCoordinator | None],
        *,
        show_system: Callable[[str], None],
        show_warning: Callable[[str], None],
    ) -> None:
        self._get_coordinator = get_coordinator
        self._show_system = show_system
        self._show_warning = show_warning

    def handle(self, command: str) -> None:
        """Executa o comando; erros de domínio viram aviso, nunca silêncio."""
        try:
            coordinator = self._get_coordinator()
        except WorkflowError as exc:
            self._show_warning(f"[workflow] {exc}")
            return
        if coordinator is None:
            self._show_warning("Workflows indisponiveis nesta sessao.")
            return
        tokens = command.split()[1:]
        action = tokens[0].lower() if tokens else "list"
        try:
            if action == "list" and len(tokens) <= 1:
                self._list(coordinator)
            elif action in {"show", "pause", "resume", "cancel"} and len(tokens) == 2:
                self._apply(coordinator, action, int(tokens[1]))
            else:
                self._show_warning(USAGE)
        except ValueError:
            self._show_warning(USAGE)
        except WorkflowError as exc:
            self._show_warning(f"[workflow] {exc}")

    def _list(self, coordinator: WorkflowCoordinator) -> None:
        workflows = coordinator.list_workflows(OPEN_WORKFLOW_STATES)
        if not workflows:
            self._show_system("Nenhum workflow aberto.")
            return
        for workflow in workflows:
            self._show_system(render_text(coordinator.snapshot(workflow.id)))

    def _apply(self, coordinator: WorkflowCoordinator, action: str, workflow_id: int) -> None:
        if action == "pause":
            coordinator.pause(workflow_id)
        elif action == "resume":
            coordinator.resume(workflow_id)
        elif action == "cancel":
            coordinator.cancel(workflow_id, reason="cancelado pelo usuario")
        self._show_system(render_text(coordinator.snapshot(workflow_id)))
