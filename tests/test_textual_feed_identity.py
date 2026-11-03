"""Identidade das execuções no modelo do feed Textual.

Cobre as linhas de tools guardadas na resposta final (toggle), o resolvedor
de execução para eventos sem run_id, delegation_id repetido, o consumo da
preview de delegação por tool_call_id e a memória limitada de execuções
encerradas.
"""
from __future__ import annotations

from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import TextualFeedModel
from tests.textual_tool_helpers import tool_msg, tool_texts


def _update(agent: str, content: str, **payload: object) -> TextualUiEvent:
    return TextualUiEvent("agent_update", {"content": content, **payload}, agent=agent)


def _preview(agent: str, content: str, **payload: object) -> TextualUiEvent:
    return TextualUiEvent("tool_preview", {"content": content, **payload}, agent=agent)


def _tool(agent: str, tool: str, args: dict[str, object] | None = None, **payload: object) -> TextualUiEvent:
    """Preview estruturada; ``tool_call_id`` no payload vai também para a linha."""
    tool_call_id = payload.get("tool_call_id")
    message = tool_msg(tool, args, tool_call_id=str(tool_call_id) if tool_call_id else None)
    return TextualUiEvent("tool_preview", {**message, **payload}, agent=agent)


def _final(agent: str, content: str, **payload: object) -> TextualUiEvent:
    return TextualUiEvent("agent_message", {"content": content, **payload}, agent=agent)


def _lifecycle(agent: str, status: str, message: str = "", **payload: object) -> TextualUiEvent:
    return TextualUiEvent(
        "agent_lifecycle",
        {"status": status, "message": message, **payload},
        agent=agent,
    )


def _delegation(delegation_id: str, *, tool_call_id: str = "", from_agent: str = "claude") -> TextualUiEvent:
    return TextualUiEvent(
        "delegation",
        {
            "from_agent": from_agent,
            "to_agent": "codex",
            "task": "revisar patch",
            "delegation_id": delegation_id,
            "tool_call_id": tool_call_id,
            "source_run_id": "",
        },
    )


def _contents(model: TextualFeedModel) -> list[tuple[str, bool, object]]:
    return [
        (
            item.event.kind,
            item.transient,
            item.event.payload.get("content") if isinstance(item.event.payload, dict) else item.event.payload,
        )
        for item in model.items
    ]


# --- tool_lines e toggle -----------------------------------------------------


def test_final_keeps_every_tool_line_of_the_run_beyond_the_visible_window():
    model = TextualFeedModel()
    model.apply(_update("claude", "trabalhando", run_id="r1"))
    for index in range(7):
        model.apply(_tool("claude", "read_file", {"path": f"f{index}.py"}, run_id="r1"))
    # O bloco vivo mostra só as últimas 5 mais a linha de excedente.
    assert len(model.items[0].event.payload["tools"]) == 6

    model.apply(_final("claude", "pronto", run_id="r1"))

    (item,) = model.items
    payload = item.event.payload
    assert tool_texts(payload["tool_lines"]) == [f"⌕ ler f{index}.py" for index in range(7)]
    assert payload["feed_seq"] == item.seq
    assert payload["tools_expanded"] is False
    assert "tools" not in payload
    assert not model._transient_tools_by_agent
    assert not model._tool_call_line_by_agent


def test_final_tool_lines_are_capped_with_overflow_line_first():
    model = TextualFeedModel()
    model.apply(_update("claude", "trabalhando", run_id="r1"))
    for index in range(250):
        model.apply(_tool("claude", "read_file", {"path": f"f{index}.py"}, run_id="r1"))

    model.apply(_final("claude", "pronto", run_id="r1"))

    lines = model.items[0].event.payload["tool_lines"]
    assert len(lines) == 200
    # Mesma posição do bloco vivo: "anteriores" fica acima das que sobraram.
    assert lines[0] == {"marker": True, "text": "⋮ +51 ferramentas anteriores"}
    assert lines[1]["text"] == "⌕ ler f51.py"
    assert lines[-1]["text"] == "⌕ ler f249.py"


def test_final_without_tools_keeps_original_event_and_is_not_toggleable():
    model = TextualFeedModel()
    model.apply(_update("claude", "pensando", run_id="r1"))
    final = _final("claude", "oi", run_id="r1")

    model.apply(final)

    assert model.items[0].event is final
    assert model.toggle_turn_tools(model.items[0].seq) is None
    assert model.latest_toggleable_seq() is None


def test_toggle_turn_tools_preserves_seq_and_flips_expansion():
    model = TextualFeedModel()
    model.apply(TextualUiEvent("user_message", {"content": "revise"}))
    model.apply(_update("claude", "trabalhando", run_id="r1"))
    model.apply(_tool("claude", "read_file", {"path": "a.py"}, run_id="r1"))
    model.apply(_final("claude", "pronto", run_id="r1"))
    before = model.items[1]
    seq = before.seq

    assert model.toggle_turn_tools(seq) == 1

    after = model.items[1]
    assert after is not before
    assert after.seq == seq
    assert after.transient is False
    assert after.event.payload["tools_expanded"] is True
    assert tool_texts(after.event.payload["tool_lines"]) == ["⌕ ler a.py"]
    assert before.event.payload["tools_expanded"] is False

    assert model.toggle_turn_tools(seq) == 1
    assert model.items[1].seq == seq
    assert model.items[1].event.payload["tools_expanded"] is False


def test_toggle_turn_tools_returns_none_for_unknown_seq_or_item_without_tools():
    model = TextualFeedModel()
    model.apply(TextualUiEvent("user_message", {"content": "oi"}))

    assert model.toggle_turn_tools(model.items[0].seq) is None
    assert model.toggle_turn_tools(-1) is None


def test_latest_toggleable_seq_is_the_most_recent_final_with_tools():
    model = TextualFeedModel()
    assert model.latest_toggleable_seq() is None
    for run_id in ("r1", "r2"):
        model.apply(_update("claude", "trabalhando", run_id=run_id))
        model.apply(_tool("claude", "read_file", {"path": f"{run_id}.py"}, run_id=run_id))
        model.apply(_final("claude", f"pronto {run_id}", run_id=run_id))
    # Final sem tools depois não rouba o atalho.
    model.apply(_update("claude", "pensando", run_id="r3"))
    model.apply(_final("claude", "sem tools", run_id="r3"))

    assert model.latest_toggleable_seq() == model.items[1].seq
    assert tool_texts(model.items[1].event.payload["tool_lines"]) == ["⌕ ler r2.py"]


def test_final_after_completed_lifecycle_keeps_the_run_tool_lines():
    # Ordem real do runner: o status "concluído" sai antes da resposta final.
    model = TextualFeedModel()
    model.apply(_lifecycle("claude", "running", "iniciando execução", run_id="r1"))
    model.apply(_tool("claude", "read_file", {"path": "a.py"}, run_id="r1"))
    model.apply(_update("claude", "texto parcial", answer=True, run_id="r1"))
    model.apply(_lifecycle("claude", "completed", "concluído", run_id="r1"))
    model.apply(_final("claude", "pronto", run_id="r1"))

    (item,) = model.items
    assert tool_texts(item.event.payload["tool_lines"]) == ["⌕ ler a.py"]
    assert model.latest_toggleable_seq() == item.seq
    assert not model._finished_tools_by_agent


def test_tool_lines_of_a_run_without_final_do_not_leak_into_the_next_run():
    model = TextualFeedModel()
    model.apply(_update("claude", "trabalhando"))
    model.apply(_tool("claude", "read_file", {"path": "velho.py"}))
    model.apply(_lifecycle("claude", "completed", "concluído"))
    # A resposta final da 1ª execução nunca chega; a 2ª começa.
    model.apply(_update("claude", "trabalhando de novo"))
    model.apply(_tool("claude", "read_file", {"path": "novo.py"}))
    model.apply(_final("claude", "pronto"))

    payload = model.items[-1].event.payload
    assert tool_texts(payload["tool_lines"]) == ["⌕ ler novo.py"]


def test_late_update_between_completed_and_final_keeps_the_tool_lines():
    model = TextualFeedModel()
    model.apply(_lifecycle("claude", "running", "iniciando execução", run_id="r1"))
    model.apply(_tool("claude", "read_file", {"path": "a.py"}, run_id="r1"))
    model.apply(_lifecycle("claude", "completed", "concluído", run_id="r1"))
    model.apply(_update("claude", "atrasado", run_id="r1"))
    model.apply(_final("claude", "pronto", run_id="r1"))

    assert tool_texts(model.items[-1].event.payload["tool_lines"]) == ["⌕ ler a.py"]


def test_simultaneous_runs_keep_their_own_tool_lines_after_completed():
    model = TextualFeedModel()
    for run_id, path in (("r1", "a.py"), ("r2", "b.py")):
        model.apply(_lifecycle("claude", "running", "iniciando execução", run_id=run_id))
        model.apply(_tool("claude", "read_file", {"path": path}, run_id=run_id))
    for run_id in ("r1", "r2"):
        model.apply(_lifecycle("claude", "completed", "concluído", run_id=run_id))
        model.apply(_final("claude", f"pronto {run_id}", run_id=run_id))

    assert [tool_texts(item.event.payload["tool_lines"]) for item in model.items] == [
        ["⌕ ler a.py"],
        ["⌕ ler b.py"],
    ]
    # O cartão da r2 manteve o run_id ao virar bloco de tools: nenhum fica órfão.
    assert not model.has_transients


def test_retry_discards_the_attempt_and_completed_keeps_the_retried_tools():
    model = TextualFeedModel()
    model.apply(_lifecycle("claude", "running", "iniciando execução", run_id="r1"))
    model.apply(_tool("claude", "read_file", {"path": "tentativa.py"}, run_id="r1"))
    model.apply(_lifecycle("claude", "retrying", "nova tentativa", run_id="r1"))
    model.apply(_tool("claude", "read_file", {"path": "final.py"}, run_id="r1"))
    model.apply(_lifecycle("claude", "completed", "concluído", run_id="r1"))
    model.apply(_final("claude", "pronto", run_id="r1"))

    assert tool_texts(model.items[-1].event.payload["tool_lines"]) == ["⌕ ler final.py"]


def test_unowned_finished_tool_lines_go_to_the_first_final():
    # Status terminal sem run_id e final com run_id: mesma regra do resumo pendente.
    model = TextualFeedModel()
    model.apply(_update("claude", "trabalhando"))
    model.apply(_tool("claude", "read_file", {"path": "a.py"}))
    model.apply(_lifecycle("claude", "completed", "concluído"))
    model.apply(_final("claude", "pronto", run_id="r1"))

    assert tool_texts(model.items[-1].event.payload["tool_lines"]) == ["⌕ ler a.py"]
    assert not model._finished_tools_by_agent


def test_finished_tool_lines_without_final_are_capped():
    model = TextualFeedModel()
    limit = TextualFeedModel._MAX_FINISHED_TOOL_RUNS
    for index in range(limit + 3):
        run_id = f"r{index}"
        model.apply(_lifecycle("claude", "running", "iniciando execução", run_id=run_id))
        model.apply(_tool("claude", "read_file", {"path": f"{run_id}.py"}, run_id=run_id))
        model.apply(_lifecycle("claude", "completed", "concluído", run_id=run_id))

    assert len(model._finished_tools_by_agent) == limit
    assert "claude#run:r0" not in model._finished_tools_by_agent


def test_restored_history_has_nothing_to_toggle():
    model = TextualFeedModel()
    model.hydrate_from_history([{"role": "human", "content": "oi"}, {"role": "claude", "content": "olá"}])

    assert model.latest_toggleable_seq() is None
    assert all(model.toggle_turn_tools(item.seq) is None for item in model.items)


def test_final_absorbing_mcp_http_burst_keeps_the_burst_tool_lines():
    model = TextualFeedModel()
    mcp = {"transport": "mcp_http", "client_name": "claude-code", "client_version": "2.0"}
    model.apply(_tool("claude", "read_file", {"path": "a.py"}, mcp_msg_id="1", **mcp))
    model.apply(_tool("claude", "grep_search", {"pattern": "x"}, mcp_msg_id="2", **mcp))

    model.apply(_final("claude", "pronto"))

    (item,) = model.items
    assert item.event.kind == "agent_message"
    assert tool_texts(item.event.payload["tool_lines"]) == ["⌕ ler a.py", '⌕ buscar "x"']


def test_final_with_run_id_absorbing_mcp_http_burst_keeps_the_burst_tool_lines():
    # Perfil HTTP: o transitório da execução (``#run:``) e o burst do cliente
    # MCP (``#mcp-client:``) são chaves distintas. A final chega com run_id e
    # substitui a execução; as tools do burst absorvido têm de ir com ela.
    model = TextualFeedModel()
    mcp = {"transport": "mcp_http", "client_name": "claude-code", "client_version": "2.0", "run_id": "r1"}
    model.apply(_update("claude", "pensando", run_id="r1"))
    model.apply(_tool("claude", "read_file", {"path": "a.py"}, mcp_msg_id="1", **mcp))
    model.apply(_tool("claude", "grep_search", {"pattern": "x"}, mcp_msg_id="2", **mcp))
    for msg_id, tool in (("1", "read_file"), ("2", "grep_search")):
        model.apply(
            TextualUiEvent(
                "tool_state",
                {**mcp, "msg_id": msg_id, "tool_name": tool, "status": "finished", "duration_ms": 4},
                agent="claude",
            )
        )
    assert sorted(model._transient_index_by_agent) == ["claude#mcp-client:claude-code@2.0", "claude#run:r1"]

    model.apply(_final("claude", "pronto", run_id="r1"))

    (item,) = model.items
    payload = item.event.payload
    assert payload["turn_summary"]["total"] == 2
    assert tool_texts(payload["tool_lines"]) == ["✓ ⌕ ler a.py · 4ms", '✓ ⌕ buscar "x" · 4ms']
    assert model.latest_toggleable_seq() == item.seq
    assert not model._transient_tools_by_agent


# --- resolvedor de execução para eventos sem run_id ---------------------------


def test_event_without_run_id_joins_the_single_live_run():
    model = TextualFeedModel()
    model.apply(_update("claude", "gerando", run_id="r1"))

    model.apply(_update("claude", "sem id"))
    model.apply(_tool("claude", "read_file", {"path": "a.py"}))

    (item,) = model.items
    assert item.transient is True
    assert item.event.payload["content"] == "sem id"
    assert item.event.payload["run_id"] == "r1"
    assert tool_texts(item.event.payload["tools"]) == ["⌕ ler a.py"]


def test_run_without_id_and_run_with_id_never_share_a_card():
    model = TextualFeedModel()
    model.apply(_update("claude", "execução sem id"))
    model.apply(_update("claude", "execução r1", run_id="r1"))
    # O transitório ``base`` é da execução sem id: o evento sem id vai para
    # ele e não sobrescreve a execução viva r1.
    model.apply(_update("claude", "sem id de novo"))

    assert _contents(model) == [
        ("agent_update", True, "sem id de novo"),
        ("agent_update", True, "execução r1"),
    ]
    assert "run_id" not in model.items[0].event.payload


def test_update_without_run_id_with_two_live_runs_is_discarded():
    model = TextualFeedModel()
    model.apply(_update("claude", "r1", run_id="r1"))
    model.apply(_update("claude", "r2", run_id="r2"))

    assert model.apply(_update("claude", "de quem?")) is False
    assert model.apply(_tool("claude", "read_file", {"path": "a.py"})) is False
    assert model.apply(_lifecycle("claude", "completed", "fim")) is False

    assert _contents(model) == [("agent_update", True, "r1"), ("agent_update", True, "r2")]
    assert all("tools" not in item.event.payload for item in model.items)


def test_final_without_run_id_with_two_live_runs_replaces_the_oldest():
    model = TextualFeedModel()
    model.apply(_update("claude", "r1", run_id="r1"))
    model.apply(_update("claude", "r2", run_id="r2"))

    assert model.apply(_final("claude", "resposta")) is True

    assert _contents(model) == [("agent_message", False, "resposta"), ("agent_update", True, "r2")]
    assert model._is_finalized_agent("claude#run:r1")
    # A execução r2 segue viva e recebe os próprios eventos.
    model.apply(_update("claude", "r2 continua", run_id="r2"))
    assert _contents(model)[1] == ("agent_update", True, "r2 continua")


def test_turn_summary_without_run_id_follows_the_single_live_run():
    model = TextualFeedModel()
    model.apply(_update("claude", "r1", run_id="r1"))
    model.apply(TextualUiEvent("turn_summary", {"total": 2, "ok_count": 2, "err_count": 0}, agent="claude"))

    model.apply(_final("claude", "pronto", run_id="r1"))

    assert model.items[0].event.payload["turn_summary"]["total"] == 2
    assert not model._pending_turn_summary_by_agent


def test_visual_reset_without_run_keeps_the_runs_listed_in_keep_run_ids():
    model = TextualFeedModel()
    model.apply(_update("claude", "r1", run_id="r1"))
    model.apply(_update("claude", "r2", run_id="r2"))

    assert model.apply(TextualUiEvent("visual_reset", {"keep_run_ids": ["r2"]}, agent="claude")) is True

    assert _contents(model) == [("agent_update", True, "r2")]


def test_retrying_lifecycle_clears_tools_without_closing_the_run():
    model = TextualFeedModel()
    model.apply(_update("claude", "trabalhando", run_id="r1"))
    model.apply(_tool("claude", "read_file", {"path": "a.py"}, run_id="r1"))

    model.apply(_lifecycle("claude", "retrying", "nova tentativa", run_id="r1"))

    (item,) = model.items
    assert item.transient is True
    assert "tools" not in item.event.payload
    assert not model._is_finalized_agent("claude#run:r1")


# --- delegation_id repetido ---------------------------------------------------


def test_duplicated_delegation_id_finishes_only_its_own_execution():
    model = TextualFeedModel()
    model.apply(_delegation("task-5"))
    model.apply(_update("codex", "tentativa 1", delegation_id="task-5", run_id="a1"))
    # Re-tentativa da task com a execução anterior ainda viva.
    model.apply(_delegation("task-5"))
    model.apply(_update("codex", "tentativa 2", delegation_id="task-5", run_id="a2"))

    assert [item.event.kind for item in model.items] == ["delegation", "agent_update", "delegation", "agent_update"]
    assert model.items[2].event.payload["delegation_occurrence"] == 2

    model.apply(_final("codex", "fim 2", delegation_id="task-5", run_id="a2"))

    assert [(kind, content) for kind, _, content in _contents(model)] == [
        ("delegation", None),
        ("agent_update", "tentativa 1"),
    ]
    assert "delegation_occurrence" not in model.items[0].event.payload

    model.apply(_final("codex", "fim 1", delegation_id="task-5", run_id="a1"))

    assert model.items == []
    assert not model._delegation_slots
    assert not model._delegation_child_slot


def test_duplicated_delegation_lifecycle_failure_closes_only_its_own_card():
    model = TextualFeedModel()
    model.apply(_delegation("task-5"))
    model.apply(_update("codex", "tentativa 1", delegation_id="task-5", run_id="a1"))
    model.apply(_delegation("task-5"))
    model.apply(_update("codex", "tentativa 2", delegation_id="task-5", run_id="a2"))

    model.apply(_lifecycle("codex", "failed", "erro", delegation_id="task-5", run_id="a1"))

    assert [(kind, content) for kind, _, content in _contents(model)] == [
        ("delegation", None),
        ("agent_update", "tentativa 2"),
    ]
    assert model.items[0].event.payload["delegation_occurrence"] == 2


def test_visual_reset_by_delegation_id_without_run_id_closes_card_and_paired_child():
    # clear_agent_transient(agent, delegation_id=…) sem run_id: a chave do
    # evento é ``codex#dlg-1``, que não é cartão nem filho pareado. O cartão
    # e a execução pareada do mesmo agente morrem juntos, como antes.
    model = TextualFeedModel()
    model.apply(_delegation("dlg-1"))
    model.apply(_update("codex", "trabalhando", delegation_id="dlg-1", run_id="a1"))

    assert model.apply(TextualUiEvent("visual_reset", {"delegation_id": "dlg-1"}, agent="codex")) is True

    assert model.items == []
    assert not model._delegation_slots
    assert not model._delegation_child_slot


def test_lifecycle_failure_by_delegation_id_without_run_id_closes_card_and_paired_child():
    model = TextualFeedModel()
    model.apply(_delegation("dlg-2"))
    model.apply(_update("codex", "trabalhando", delegation_id="dlg-2", run_id="b1"))

    assert model.apply(_lifecycle("codex", "failed", "erro", delegation_id="dlg-2")) is True

    assert model.items == []
    # A execução que caiu fica finalizada: uma preview atrasada não reabre nada.
    assert model.apply(_tool("codex", "read_file", {"path": "tarde.py"}, delegation_id="dlg-2", run_id="b1")) is False
    assert model.items == []


def test_close_by_delegation_id_without_run_id_takes_the_oldest_card_of_that_agent():
    model = TextualFeedModel()
    model.apply(_delegation("task-5"))
    model.apply(_update("codex", "tentativa 1", delegation_id="task-5", run_id="a1"))
    model.apply(_delegation("task-5"))
    model.apply(_update("codex", "tentativa 2", delegation_id="task-5", run_id="a2"))

    model.apply(_lifecycle("codex", "failed", "erro", delegation_id="task-5"))

    assert [(kind, content) for kind, _, content in _contents(model)] == [
        ("delegation", None),
        ("agent_update", "tentativa 2"),
    ]
    assert model._delegation_child_slot == {"codex#run:a2": "__global__#task-5#2"}


def test_close_by_delegation_id_of_another_agent_keeps_the_paired_child():
    # Cartão pareado ao codex; um reset do gemini com o mesmo delegation_id
    # (chave ``gemini#dlg-3``) não tem cartão próprio e não derruba o do codex.
    model = TextualFeedModel()
    model.apply(_delegation("dlg-3"))
    model.apply(_update("codex", "trabalhando", delegation_id="dlg-3", run_id="c1"))

    assert model.apply(TextualUiEvent("visual_reset", {"delegation_id": "dlg-3"}, agent="gemini")) is False

    assert [item.event.kind for item in model.items] == ["delegation", "agent_update"]


def test_delegation_shown_again_before_its_execution_does_not_duplicate_the_card():
    model = TextualFeedModel()
    model.apply(_delegation("dlg-1"))
    model.apply(_delegation("dlg-1"))

    assert [item.event.kind for item in model.items] == ["delegation"]
    assert model._delegation_slots == {"dlg-1": ["__global__#dlg-1"]}


# --- preview de tasks_delegate consumida por tool_call_id ---------------------


def _two_orchestrators_delegating_same_task(model: TextualFeedModel) -> None:
    delegate = {"target_agent": "codex", "request": "revisar patch"}
    for agent, call_id in (("alpha", "call-a"), ("beta", "call-b")):
        model.apply(_update(agent, f"{agent} delegando", run_id=f"{agent}-run"))
        model.apply(_tool(agent, "tasks_delegate", delegate, run_id=f"{agent}-run", tool_call_id=call_id))


def test_delegation_consumes_only_the_preview_of_its_own_tool_call():
    model = TextualFeedModel()
    _two_orchestrators_delegating_same_task(model)

    model.apply(_delegation("dlg-b", tool_call_id="call-b", from_agent="beta"))

    alpha, beta = model.items[0].event.payload, model.items[1].event.payload
    assert tool_texts(alpha["tools"]) == ["⇄ delegar codex · revisar patch"]
    assert "tools" not in beta
    assert model.items[2].event.kind == "delegation"
    assert "call-b" not in model._tool_call_line_by_agent.get("beta#run:beta-run", {})


def test_delegation_consumes_the_preview_even_when_the_orchestrator_name_differs_in_case():
    # A tool normaliza o nome do orquestrador em minúsculas; a preview chega
    # com o nome do perfil como está.
    model = TextualFeedModel()
    model.apply(_update("Claude", "delegando", run_id="r1"))
    model.apply(_tool("Claude", "tasks_delegate", {"target_agent": "codex", "request": "revisar"}, run_id="r1", tool_call_id="call-1"))

    model.apply(_delegation("dlg-1", tool_call_id="call-1", from_agent="claude"))

    assert "tools" not in model.items[0].event.payload
    assert model.items[1].event.kind == "delegation"


def test_anonymous_http_orchestrator_consumes_its_preview_by_tool_call_id_alone():
    # Cliente HTTP sem identidade: o cartão vem como "agente" e a preview como
    # "mcp-http". Sem execução do "agente" no feed, vale o tool_call_id.
    model = TextualFeedModel()
    model.apply(_update("mcp-http", "delegando", run_id="h1"))
    model.apply(_tool("mcp-http", "tasks_delegate", {"target_agent": "codex", "request": "revisar"}, run_id="h1", tool_call_id="call-h"))

    model.apply(_delegation("dlg-h", tool_call_id="call-h", from_agent="agente"))

    assert "tools" not in model.items[0].event.payload


def test_anonymous_orchestrator_fallback_only_consumes_a_tasks_delegate_line():
    # Sem execução do orquestrador, o id sozinho não pode tirar uma tool
    # qualquer de outro agente: ids sequenciais de dois perfis podem bater.
    model = TextualFeedModel()
    model.apply(_update("gemini", "lendo", run_id="g1"))
    model.apply(_tool("gemini", "read_file", {"path": "a.py"}, run_id="g1", tool_call_id="call_0"))

    model.apply(_delegation("dlg-0", tool_call_id="call_0", from_agent="agente"))

    assert tool_texts(model.items[0].event.payload["tools"]) == ["⌕ ler a.py"]


def test_orchestrator_with_live_runs_never_consumes_another_orchestrator_preview():
    model = TextualFeedModel()
    _two_orchestrators_delegating_same_task(model)

    # beta tem execução viva mas o id é do alpha: o fallback global não vale.
    model.apply(_delegation("dlg-x", tool_call_id="call-a", from_agent="beta"))

    alpha, beta = model.items[0].event.payload, model.items[1].event.payload
    assert tool_texts(alpha["tools"]) == ["⇄ delegar codex · revisar patch"]
    assert tool_texts(beta["tools"]) == ["⇄ delegar codex · revisar patch"]


def test_delegation_with_source_run_id_consumes_the_preview_of_that_run_only():
    model = TextualFeedModel()
    delegate = {"target_agent": "codex", "request": "revisar"}
    for run in ("r1", "r2"):
        model.apply(_update("claude", f"execução {run}", run_id=run))
        model.apply(_tool("claude", "tasks_delegate", delegate, run_id=run, tool_call_id="call-same"))

    event = _delegation("dlg-2", tool_call_id="call-same", from_agent="claude")
    event.payload["source_run_id"] = "r2"
    model.apply(event)

    first, second = model.items[0].event.payload, model.items[1].event.payload
    assert tool_texts(first["tools"]) == ["⇄ delegar codex · revisar"]
    assert "tools" not in second


def test_delegation_without_tool_call_id_consumes_nothing():
    model = TextualFeedModel()
    _two_orchestrators_delegating_same_task(model)

    model.apply(_delegation("dlg-x", tool_call_id="", from_agent="beta"))

    assert tool_texts(model.items[0].event.payload["tools"]) == ["⇄ delegar codex · revisar patch"]
    assert tool_texts(model.items[1].event.payload["tools"]) == ["⇄ delegar codex · revisar patch"]


def test_delegation_steps_with_same_tool_call_id_consume_the_preview_once():
    model = TextualFeedModel()
    model.apply(_update("alpha", "delegando", run_id="r1"))
    model.apply(_tool("alpha", "read_file", {"path": "a.py"}, run_id="r1", tool_call_id="call-r"))
    model.apply(_tool("alpha", "tasks_delegate", {"target_agent": "codex", "request": "passo"}, run_id="r1", tool_call_id="call-d"))

    model.apply(_delegation("dlg-1", tool_call_id="call-d", from_agent="alpha"))
    model.apply(_delegation("dlg-2", tool_call_id="call-d", from_agent="alpha"))

    assert tool_texts(model.items[0].event.payload["tools"]) == ["⌕ ler a.py"]


def test_tool_call_map_is_dropped_with_the_run():
    model = TextualFeedModel()
    model.apply(_update("alpha", "trabalhando", run_id="r1"))
    model.apply(_tool("alpha", "read_file", {"path": "a.py"}, run_id="r1", tool_call_id="call-1"))
    entry = model._tool_call_line_by_agent["alpha#run:r1"]["call-1"]
    assert list(model._tool_call_line_by_agent) == ["alpha#run:r1"]
    assert entry is model._transient_tools_by_agent["alpha#run:r1"][0]

    model.apply(_final("alpha", "pronto", run_id="r1"))
    assert not model._tool_call_line_by_agent

    model.apply(_update("beta", "trabalhando", run_id="r2"))
    model.apply(_tool("beta", "read_file", {"path": "b.py"}, run_id="r2", tool_call_id="call-2"))
    model.clear_chat()
    assert not model._tool_call_line_by_agent


# --- memória de execuções encerradas -----------------------------------------


def test_finalized_runs_are_remembered_as_bounded_lru():
    model = TextualFeedModel()
    model._MAX_FINALIZED_AGENTS = 3
    for index in range(5):
        model.apply(_update("claude", "trabalhando", run_id=f"r{index}"))
        model.apply(_final("claude", "pronto", run_id=f"r{index}"))

    assert list(model._finalized_agents) == ["claude#run:r2", "claude#run:r3", "claude#run:r4"]

    # Run encerrada que volta a falar sai da memória e volta a ser exibida.
    model.apply(_update("claude", "de novo", run_id="r3"))
    assert not model._is_finalized_agent("claude#run:r3")
    assert model.items[-1].transient is True


def test_late_event_of_a_finalized_run_is_ignored():
    model = TextualFeedModel()
    model.apply(_update("claude", "trabalhando", run_id="r1"))
    model.apply(_final("claude", "pronto", run_id="r1"))

    assert model.apply(_tool("claude", "read_file", {"path": "tarde.py"}, run_id="r1")) is False
    assert model.apply(_lifecycle("claude", "completed", "fim", run_id="r1")) is False
    assert [item.event.kind for item in model.items] == ["agent_message"]
