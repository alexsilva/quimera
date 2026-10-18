# Contrato de Interação com Agente na UI Textual + MCP Socket

## 1. Fluxo de Chat Normal

```
Textual Input.Submitted
  -> TextualUiBridge.submit_input           # quimera/ui/textual/bridge.py
     -> input_queue.put(value)              # rota normal de chat
  -> TextualInputGate.__call__              # quimera/ui/textual/input_gate.py
     -> bloqueia em input_queue.get()
  -> app.read_user_input                    # quimera/app/core_facade.py → AppInputServices (quimera/app/inputs.py)
  -> chat_processor                         # loop principal do app
  -> ChatLifecycle.process_message          # quimera/app/chat_lifecycle.py
  -> ChatRoundOrchestrator.process          # quimera/app/chat_round.py
  -> AppDispatchServices.delegate           # quimera/app/dispatch.py — roteia para o agente alvo
  -> AgentGateway.call                      # quimera/app/agent_gateway.py
  -> AgentClient.call                       # quimera/agents/client.py
  -> renderer/feed                          # saída streamada para a UI
```

**Invariante:** `submit_input` só enfileira em `input_queue` quando `is_direct_input_active()` é `False` e não há agente ativo para injeção de stdin.

## 2. Fluxo Modal (Approval / Question / Selection)

```
Modal ativo (approval/question/selection)
  -> TextualInputGate._read_with_textual_prompt   # quimera/ui/textual/input_gate.py
     -> bridge.begin_direct_input()                # incrementa _direct_input_depth
     -> bridge.emit("question", {...})             # overlay visual
     -> bridge.direct_input_queue.get()            # bloqueia até resposta
  -> consumidor específico (approval_handler / read_selection_in_terminal / read_input_in_terminal)
  -> bridge.end_direct_input()                     # decrementa _direct_input_depth
```

**Regra:** Toda submissão durante `is_direct_input_active() == True` cai em `direct_input_queue` — **nunca** vira mensagem de chat nem passa pelo `chat_processor`.

**Caminhos que ativam direct input:**
- `read_approval_in_terminal` → approval (y/s/a/n)
- `read_selection_in_terminal` → seleção numerada
- `read_input_in_terminal` → prompt livre inline
- Eventos `"question"`, `"window_open"`, `"pending_input"` **não** ativam direct input: são só visuais (ver seção 5, "Separação importante")

## 3. Fluxo Agent Stdin

```
submit_input(value)
  -> _try_inject_active_agent(text)               # quimera/ui/textual/bridge.py
     -> quimera_app.is_agent_running?              # só quando há subprocesso ativo
     -> quimera_app.active_agent_stdin.write()     # escreve direto no stdin do agente
     -> retorna True (consome o input)
```

**Só recebe input quando:**
1. Não há modal ativo (`is_direct_input_active() == False`).
2. Não há approval ativo no executor.
3. Há agente/processo ativo aceitando stdin (`quimera_app.is_agent_running` e `active_agent_stdin` não `None`).

Se o input for consumido pelo agente, **não** cai em `input_queue` nem `direct_input_queue`.

## 4. Fluxo MCP Socket Tool

```
Agente chama tools/call via MCP socket
  -> MCPServer._handle_tools_call                  # quimera/runtime/mcp/server.py
     -> constrói TrustedToolExecutionContext
     -> submete para thread pool:
        ToolExecutor.execute                       # quimera/runtime/executor.py
          -> policy.requires_validation
          -> policy.requires_approval
          -> _tool_preview_callback(name, args, metadata)   # preview operacional SEMPRE
          -> ApprovalBroker.approve                # se precisa aprovação
             -> auto-approve? (scope/policy)
             -> approval_handler.approve()         # card laranja + input humano
          -> registry.get(name)(call)              # executa handler real
          -> resultado volta via JSON-RPC response
```

**Invariantes do preview:**
- Preview é emitido **antes** do `ApprovalBroker.approve`, sem qualquer guard condicional.
- Tool READ (auto-aprovada) → preview + execução imediata, sem card laranja.
- Tool WRITE com approval → preview + card laranja + aguarda `y/s/a/n`.
- Tool negada → preview é emitido, handler **não** executa, retorna erro.

## 5. Fluxo de Feed de Mensagens (Runtime → UI)

```
Agente/Executor produz saída
  -> TextualRenderer.show_message / show_feed / show_approval / show_plain   # quimera/ui/textual/renderer.py
     -> bridge.emit(TextualUiEvent(kind, payload, agent))     # quimera/ui/textual/bridge.py
        -> call_from_thread(textual_app.handle_bridge_event)  # enfileira no event loop do Textual
        -> se app não attached: fallback para ui_queue interna
  -> QuimeraTextualApp.handle_bridge_event                    # ui/textual/app.py
     -> eventos especiais:
        "clear"       -> feed_model.clear_chat()   # mantém o cabeçalho de boot
        "question"    -> _set_question_overlay()
        "question_clear" -> overlay hidden
        "window_open" -> _set_question_overlay(_build_window_overlay_payload(...))
        "window_clear" -> overlay hidden
        "prompt_clear" -> reset estado active do gate
        "summarizing" -> card de sumarização
     -> outros eventos passam por:
        TextualFeedModel.apply(event)                          # quimera/ui/textual/feed_model.py
           -> se transient kind: upsert/substitui item
           -> se final: remove transiente correspondente
           -> se permanente: append
        -> _render_event(event)                                # quimera/ui/textual/renderables.py
           -> Rich renderable (cacheado por evento) montado nos slots do feed
```

**Regras do feed model:**
- `TextualFeedModel._TRANSIENT_KINDS` (`agent_update`, `agent_lifecycle`, `pending_input`): itens substituíveis que desaparecem quando o final chega.
- Quando o agente pede input (`pending_input`), a resposta parcial que estava no transitório (`agent_update` com `answer`) vira mensagem fixa antes do cartão da pergunta; as atualizações seguintes, cumulativas, mostram só o trecho novo.
- Itens muted (tool preview) são permanentes: uma vez emitidos, ficam no feed.
- `visual_reset` no sync: remove todos os transientes do agente atual.

**Separação importante:**
- Eventos visuais (`question`, `window_open`, `pending_input`) não armam roteamento por si só.
- O roteamento modal é armado pelo consumidor real (`_read_with_textual_prompt()` / `_interactive_window()`) antes de esperar `direct_input_queue`.
- Isso impede que um evento visual desvie input normal do chat para a fila modal.

**Dreno periódico:**
- `_bridge_drain_timer` (50ms) na `on_mount` coleta eventos que chegaram antes do Textual estar pronto.
- Garante que nenhum evento se perca na janela entre `bridge.emit` e `handle_bridge_event` registrar o callback.

## 6. Invariantes do Sistema

| Invariante | Garantido por |
|---|---|
| Chat input normal sempre chega ao `chat_processor` | `submit_input` só roteia para `input_queue` quando não há direct input ativo nem agente stdin |
| Approval consome y/s/a/n | `read_approval_in_terminal` → `_read_with_textual_prompt` → `direct_input_queue` (fora do chat) |
| Preview de tool MCP socket aparece com ou sem approval | `execute()` emite preview antes de qualquer decisão de approval |
| Approval visual (card laranja) aparece apenas quando há pedido humano real | `ApprovalBroker.approve` só chama `approval_handler.approve` quando `_can_auto_approve` retorna `False` |
| `direct_input`/`modal state` não é proxy genérico para "input não-chat" | `is_direct_input_active()` reflete exclusivamente `_direct_input_depth > 0`, controlado por `begin_direct_input`/`end_direct_input` (contagem por thread dona; threads mortas são descartadas) |
