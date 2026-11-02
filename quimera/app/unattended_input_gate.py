"""Input gate de uma execução sem humano (``quimera --prompt``).

Não há quem responda: o stdin pertence ao chamador (o prompt pode ter vindo
dele com ``--prompt -``) e nenhuma leitura o consome. O ``InputBroker`` lê
``unattended_approval`` e responde sozinho: aprovações seguem a política
(``deny`` nega, ``auto`` aprova) e ``ask_user`` falha na hora, sem esperar o
timeout de um humano ausente.
"""
from __future__ import annotations

import io
from typing import Literal

from .simple_input_gate import SimpleInputGate

UnattendedApproval = Literal["deny", "auto"]


class UnattendedInputGate(SimpleInputGate):
    """``SimpleInputGate`` sobre um stdin vazio, com a política de aprovação."""

    def __init__(
        self,
        renderer=None,
        toolbar_context_resolver=None,
        history_file=None,
        command_resolver=None,
        argument_resolver=None,
        *,
        approval: UnattendedApproval = "deny",
    ) -> None:
        if approval not in {"deny", "auto"}:
            raise ValueError(f"política de aprovação inválida: {approval!r}")
        super().__init__(
            renderer=renderer,
            toolbar_context_resolver=toolbar_context_resolver,
            history_file=history_file,
            command_resolver=command_resolver,
            argument_resolver=argument_resolver,
            stdin=io.StringIO(""),
        )
        self.unattended_approval: UnattendedApproval = approval
