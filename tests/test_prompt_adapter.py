"""Testes puros do adapter de prompt para payload OpenAI-compatible."""
from __future__ import annotations

import pytest

from quimera.prompt_kinds import PromptKind
from quimera.prompt_templates import PromptText
from quimera.runtime.drivers.prompt_adapter import (
    _build_openai_messages_from_prompt,
    _build_tool_budget_prompt,
    _build_tool_system_prompt,
)


def _rendered(text="", kind=PromptKind.CHAT):
    return PromptText(text, kind)


def test_build_openai_messages_from_prompt_uses_current_turn_as_active_user_message():
    """current_turn vira a última mensagem user ativa."""
    prompt = (
        '<rules title="Suas regras">contexto</rules>\n'
        '<recent_conversation title="Conversa recente">\n'
        'USER: Leia o README\nASSISTANT: já li\n'
        '</recent_conversation>\n'
        '<current_turn title="Pedido atual de >>>">\n'
        'Execute pwd via shell usando MCP\n'
        '</current_turn>'
    )

    messages = _build_openai_messages_from_prompt(_rendered(prompt))

    assert messages[-1] == {"role": "user", "content": "Execute pwd via shell usando MCP"}
    assert all(message["role"] == "system" for message in messages[:-1])
    assert "Leia o README" in messages[-2]["content"]
    assert "Execute pwd" not in messages[0]["content"]


def test_build_openai_messages_returns_empty_for_blank_prompt():
    """Prompt vazio não gera mensagem user vazia."""
    assert _build_openai_messages_from_prompt(_rendered("")) == []
    assert _build_openai_messages_from_prompt(_rendered("  \n\t  ")) == []


def test_build_openai_messages_keeps_current_turn_last_with_embedded_xml():
    """XML/HTML dentro de current_turn não é interpretado como bloco do template."""
    prompt = (
        '<recent_conversation title="Conversa recente">\n'
        'Histórico anterior\n'
        '</recent_conversation>\n'
        '<current_turn title="Pedido atual">\n'
        'Analise este HTML/XML:\n'
        '```html\n'
        '<section>\n'
        '<recent_conversation>não é histórico</recent_conversation>\n'
        '</section>\n'
        '```\n'
        '</current_turn>\n'
    )

    messages = _build_openai_messages_from_prompt(_rendered(prompt))

    assert messages[-1]["role"] == "user"
    assert "Analise este HTML/XML" in messages[-1]["content"]
    assert "<recent_conversation>não é histórico</recent_conversation>" in messages[-1]["content"]
    assert messages[-1]["content"].count("não é histórico") == 1


def test_build_openai_messages_keeps_current_turn_last_and_omits_metrics():
    """current_turn continua último, mas agent_metrics não entra no payload OpenAI."""
    prompt = (
        '<header title="Identificação">contexto</header>\n'
        '<current_turn title="Pedido atual">pedido atual</current_turn>\n'
        '<agent_metrics title="Métricas">métricas</agent_metrics>'
    )

    messages = _build_openai_messages_from_prompt(_rendered(prompt))

    assert messages[-1] == {"role": "user", "content": "pedido atual"}
    assert all(message["role"] == "system" for message in messages[:-1])
    assert all("métricas" not in message["content"] for message in messages)


def test_build_openai_messages_preserves_template_order_and_omits_metrics():
    """Adapter preserva ordem do template; agent_metrics é omitido."""
    prompt = (
        '<header title="Identificação">\nHEADER\n</header>\n'
        '<handoff title="Mensagem direta do outro agente">\nHANDOFF\n</handoff>\n'
        '<debug_state title="Debug de render ativo">\nDEBUG\n</debug_state>\n'
        '<agent_metrics title="Métricas">\nMETRICS\n</agent_metrics>\n'
        '<recent_conversation title="Conversa recente">\nHISTORY\n</recent_conversation>\n'
        '<current_turn title="Pedido atual">\nCURRENT\n</current_turn>'
    )

    messages = _build_openai_messages_from_prompt(_rendered(prompt, PromptKind.CHAT))

    assert [message["content"] for message in messages] == [
        "Identificação\n\nHEADER",
        "HANDOFF",
        "Debug de render ativo\n\nDEBUG",
        "Conversa recente\n\nHISTORY",
        "CURRENT",
    ]


def test_build_openai_messages_rejects_free_text_outside_blocks():
    """Texto fora de blocos é bug de template, não contexto operacional implícito."""
    with pytest.raises(ValueError, match="Texto fora de blocos"):
        _build_openai_messages_from_prompt(
            _rendered('texto solto\n<header title="H">\nctx\n</header>')
        )


def test_build_openai_messages_uses_plain_titles_without_instructional_text():
    """Títulos vêm do atributo title, sem texto instrucional extra."""
    prompt = (
        '<header title="Identificação">\nVocê é OPENAI.\n</header>\n'
        '<recent_conversation title="Conversa recente">\nUSER: ação antiga\n</recent_conversation>\n'
        '<current_turn title="Pedido atual de >>>">\nAção atual\n</current_turn>'
    )

    messages = _build_openai_messages_from_prompt(_rendered(prompt, PromptKind.CHAT))

    assert messages[0]["content"] == "Identificação\n\nVocê é OPENAI."
    assert messages[1]["content"] == "Conversa recente\n\nUSER: ação antiga"
    assert "Não trate este bloco" not in messages[0]["content"]
    assert "Use para evitar duplicação" not in messages[1]["content"]


def test_build_openai_messages_uses_title_attribute_instead_of_raw_tag():
    """Blocos system usam title=... como título, não a tag renderizada inteira."""
    prompt = '<rules title="Suas regras">\n- Faça o certo.\n</rules>'

    messages = _build_openai_messages_from_prompt(_rendered(prompt, PromptKind.CHAT))

    assert messages == [{"role": "system", "content": "Suas regras\n\n- Faça o certo."}]
    assert "<rules" not in messages[0]["content"]


def test_build_openai_messages_keeps_debug_state_when_rendered_for_chat():
    """debug_state chega ao OpenAI quando o prompt renderizado o contém."""
    prompt = (
        '<debug_state title="Debug de render ativo">\n'
        '- Eventos estruturados: /tmp/render.jsonl\n'
        '</debug_state>\n'
        '<current_turn title="Pedido atual">\nInvestigue o bug visual.\n</current_turn>'
    )

    messages = _build_openai_messages_from_prompt(_rendered(prompt, PromptKind.CHAT))

    assert messages[0] == {
        "role": "system",
        "content": "Debug de render ativo\n\n- Eventos estruturados: /tmp/render.jsonl",
    }
    assert messages[-1] == {"role": "user", "content": "Investigue o bug visual."}


def test_build_openai_messages_uses_prompt_kind_policy_for_task_executor():
    """Política task_executor preserva debug/rules da task e omite blocos de chat."""
    prompt = (
        '<debug_state title="Debug de render ativo">\nDEBUG\n</debug_state>\n'
        '<rules title="Suas regras">\nRegra de chat que não deve entrar.\n</rules>\n'
        '<task_execution_rules title="Protocolo operacional">\n- Leia o alvo.\n</task_execution_rules>\n'
        '<task_handoff title="Task atribuída">\nTASK:\nEditar.\n</task_handoff>'
    )

    messages = _build_openai_messages_from_prompt(_rendered(prompt, PromptKind.TASK_EXECUTOR))

    contents = [message["content"] for message in messages]
    assert "Debug de render ativo\n\nDEBUG" in contents
    assert "Protocolo operacional\n\n- Leia o alvo." in contents
    assert messages[-1] == {"role": "user", "content": "TASK:\nEditar."}
    assert all("Regra de chat" not in content for content in contents)


def test_build_openai_messages_uses_prompt_kind_policy_for_task_reviewer():
    """Política task_reviewer preserva debug/rules de review e material de validação."""
    prompt = (
        '<debug_state title="Debug de render ativo">\nDEBUG\n</debug_state>\n'
        '<task_review_rules title="Critério de review">\n- Responda ACEITE ou RETENTATIVA.\n</task_review_rules>\n'
        '<task_review title="Material para validação">\nTASK:\nValidar.\n</task_review>'
    )

    messages = _build_openai_messages_from_prompt(_rendered(prompt, PromptKind.TASK_REVIEWER))

    contents = [message["content"] for message in messages]
    assert "Debug de render ativo\n\nDEBUG" in contents
    assert "Critério de review\n\n- Responda ACEITE ou RETENTATIVA." in contents
    assert messages[-1] == {"role": "user", "content": "TASK:\nValidar."}


def test_build_openai_messages_maps_task_reviewer_rules_to_system_and_material_to_user():
    """Task reviewer mapeia regras para system e material para user."""
    prompt = (
        '<header title="Task Reviewer">\nVocê é OPENAI.\n</header>\n'
        '<task_review_rules title="Critério de review">\n'
        '- Responda com ACEITE ou RETENTATIVA.\n'
        '</task_review_rules>\n'
        '<task_review title="Material para validação">\n'
        'TASK:\nValidar execução\n'
        '</task_review>'
    )

    messages = _build_openai_messages_from_prompt(_rendered(prompt, PromptKind.TASK_REVIEWER))

    assert messages[-1] == {"role": "user", "content": "TASK:\nValidar execução"}
    assert all(message["role"] == "system" for message in messages[:-1])
    assert "Critério de review" in messages[1]["content"]
    assert "ACEITE ou RETENTATIVA" in messages[1]["content"]


def test_build_tool_system_prompt_includes_workspace_hint():
    """Prompt de ferramentas permanece curto e sem lista explícita de ferramentas."""
    prompt = _build_tool_system_prompt(["read_file", "apply_patch"], "/tmp/workspace")

    assert "Use as ferramentas disponíveis" in prompt
    assert "não repita o mesmo payload inválido" in prompt
    assert "read_file, apply_patch" not in prompt
    assert "Workspace raiz: /tmp/workspace." not in prompt


def test_build_tool_system_prompt_avoids_unavailable_tool_guidance():
    """Prompt curto não injeta orientação específica de tools individuais."""
    prompt = _build_tool_system_prompt(["read_file"], "/tmp/workspace")

    assert "ferramentas disponíveis" in prompt
    assert "read_file usa 'path', não 'file_path'" not in prompt
    assert "run_shell" not in prompt
    assert "exec_command" not in prompt
    assert "começar exatamente com '*** Begin Patch'" not in prompt


def test_build_tool_system_prompt_prefers_call_agent_for_delegation():
    """Prompt curto não duplica instruções específicas de call_agent."""
    prompt = _build_tool_system_prompt(["read_file", "call_agent"], "/tmp/workspace")

    assert "ferramentas disponíveis" in prompt
    assert "Para delegação entre agentes, use a tool `call_agent`" not in prompt
    assert "use `fallback_agents` para failover sequencial" not in prompt
    assert "e `handoffs` para múltiplos passos no mesmo envio" not in prompt
    assert "Se precisar delegar e `call_agent` não estiver disponível" not in prompt


def test_build_tool_system_prompt_reports_limitation_without_call_agent():
    """Prompt curto não injeta limitação específica quando call_agent não está disponível."""
    prompt = _build_tool_system_prompt(["read_file"], "/tmp/workspace")

    assert "ferramentas disponíveis" in prompt
    assert "Se precisar delegar e `call_agent` não estiver disponível" not in prompt


def test_build_tool_system_prompt_includes_shell_policy_rules():
    """Prompt curto não duplica política detalhada de shell no system prompt."""
    prompt = _build_tool_system_prompt(
        ["run_shell", "exec_command"],
        "/tmp/workspace",
        shell_allowlist=["ls", "cat", "pytest"],
    )

    assert "ferramentas disponíveis" in prompt
    assert "sem operadores de encadeamento" not in prompt
    assert "comandos permitidos" not in prompt


def test_build_tool_budget_prompt_includes_max_and_remaining():
    """Budget prompt inclui máximo e restante de tool hops."""
    prompt = _build_tool_budget_prompt(max_tool_hops=24, remaining_tool_hops=17)

    assert "max_tool_hops=24" in prompt
    assert "remaining_tool_hops=17" in prompt
