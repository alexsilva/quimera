"""Testes para o aviso de truncamento de request/context na tool delegate.

Antes, request acima de 1200 caracteres e context acima de 4000 eram cortados
silenciosamente — nem o chamador nem o agente alvo ficavam sabendo. Agora:
  - o payload truncado carrega um marcador embutido para o agente alvo;
  - o resultado da tool traz aviso em content e em data["truncation_warnings"];
  - o comprimento final respeita exatamente o limite.

Execute com:
  pytest tests/test_delegate_truncation.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.workspace import Workspace
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.delegate import DelegateTools

MAX_REQUEST = DelegateTools._DELEGATE_MAX_REQUEST_CHARS
MAX_CONTEXT = DelegateTools._DELEGATE_MAX_CONTEXT_CHARS


@pytest.fixture
def dispatch_fn():
    return MagicMock(return_value="resultado do agente")


@pytest.fixture
def delegation_tools(tmp_path, dispatch_fn):
    config = ToolRuntimeConfig(workspace=Workspace(tmp_path))
    tools = DelegateTools(config)
    tools.set_delegate_fn(dispatch_fn)
    return tools


def _make_call(args: dict) -> ToolCall:
    return ToolCall(name="delegate", arguments=args, metadata={})


class TestTruncateDelegateText:
    """Testes unitários do helper _truncate_delegate_text."""

    def test_texto_dentro_do_limite_passa_intacto(self):
        text, warning = DelegateTools._truncate_delegate_text("abc", 10, "request")
        assert text == "abc"
        assert warning is None

    def test_texto_no_limite_exato_passa_intacto(self):
        text, warning = DelegateTools._truncate_delegate_text("a" * 10, 10, "request")
        assert text == "a" * 10
        assert warning is None

    def test_texto_acima_do_limite_recebe_marcador_e_aviso(self):
        original = "x" * (MAX_REQUEST + 500)
        text, warning = DelegateTools._truncate_delegate_text(
            original, MAX_REQUEST, "request",
        )
        assert len(text) == MAX_REQUEST
        assert "truncado pela tool delegate" in text
        assert str(MAX_REQUEST + 500) in text
        assert warning is not None
        assert "request" in warning
        assert str(MAX_REQUEST) in warning


class TestDelegateRequestTruncation:
    """Truncamento do request principal deve gerar aviso visível."""

    def test_request_longo_gera_aviso_no_resultado(self, delegation_tools, dispatch_fn):
        call = _make_call({
            "target_agent": "codex",
            "request": "faz algo " * 400,
        })
        result = delegation_tools.delegate(call)
        assert result.ok
        assert "Aviso de truncamento" in result.content
        assert "resultado do agente" in result.content
        warnings = result.data.get("truncation_warnings")
        assert warnings and len(warnings) == 1
        assert "request" in warnings[0]

    def test_request_truncado_chega_com_marcador_ao_agente(self, delegation_tools, dispatch_fn):
        call = _make_call({
            "target_agent": "codex",
            "request": "faz algo " * 400,
        })
        delegation_tools.delegate(call)
        delegation = dispatch_fn.call_args.kwargs["delegation"]
        assert len(delegation["task"]) == MAX_REQUEST
        assert "truncado pela tool delegate" in delegation["task"]

    def test_context_longo_gera_aviso(self, delegation_tools, dispatch_fn):
        call = _make_call({
            "target_agent": "codex",
            "request": "faz algo",
            "context": "c" * (MAX_CONTEXT + 1),
        })
        result = delegation_tools.delegate(call)
        assert result.ok
        warnings = result.data.get("truncation_warnings")
        assert warnings and "context" in warnings[0]
        delegation = dispatch_fn.call_args.kwargs["delegation"]
        assert len(delegation["context"]) == MAX_CONTEXT

    def test_sem_truncamento_nao_ha_aviso(self, delegation_tools, dispatch_fn):
        call = _make_call({
            "target_agent": "codex",
            "request": "faz algo",
            "context": "contexto curto",
        })
        result = delegation_tools.delegate(call)
        assert result.ok
        assert result.content.startswith("resultado do agente")
        assert "truncado" not in result.content
        assert "truncation_warnings" not in result.data


class TestDelegateStepsTruncation:
    """Truncamento em steps extras deve identificar o step no aviso."""

    def test_step_extra_truncado_gera_aviso_rotulado(self, delegation_tools, dispatch_fn):
        call = _make_call({
            "target_agent": "codex",
            "request": "faz algo",
            "steps": [
                {
                    "target_agent": "sonnet",
                    "request": "r" * (MAX_REQUEST + 1),
                    "context": "c" * (MAX_CONTEXT + 1),
                },
            ],
        })
        result = delegation_tools.delegate(call)
        assert result.ok
        warnings = result.data.get("truncation_warnings")
        assert warnings and len(warnings) == 2
        assert any("steps[0].context" in w for w in warnings)
        assert any("steps[0].request" in w for w in warnings)
