# Workflow Completo: `/task` → Execução → Review → Fim

Gerado a partir de análise do código. Documenta o fluxo de dados e pontos de intervenção.

---

## 1. CRIAÇÃO (`/task "descreve..."`)

```
core.py:730 handle_command() → system_layer.py:398 CMD_TASK
  → task.py:219 handle_task_command()
    1. parse_task_command()           → extrai descrição
    2. classify_task_type()          → infere tipo (code_edit, etc)
    3. choose_agent_with_load_balance() → roteia agente
    4. repository.create_task()      → SQLite (status=pending)
    5. build_task_body()             → monta prompt da task
    6. refresh_task_shared_state()   → atualiza shared_state
    7. system_layer.show_system_message() → "[task N] criada com id N"
```

Problema: única mensagem visível, sem previsibilidade.

---

## 2. EXECUTOR BACKGROUND (thread separada)

```
task.py:65-78 setup_task_executors()
  → create_executor(agent, task_handler)
    → Thread executa task_handler(task) quando task.status == pending
```

Problema: executor roda em thread silenciosa. Humano sem visibilidade de que a task começou a executar.

---

## 3. EXECUTION SERVICE (`task_execution_service.py`)

```
handler_for(agent) → task_handler(task):

1. system_layer.show_system_message() → "[task N] agente: iniciando"
   ─── SUPRIMIDA por _SUPPRESSED_TASK_STATUS_FRAGMENTS (": iniciando") ───

2. dispatch_services.call_agent(
      agent, handoff=prompt,
      handoff_only=True,  primary=False,
      silent=True,         ← SUPRIME streaming do gateway
      show_output=False,   ← SUPRIME resposta do agente na tela
      persist_history=False
    )

3. dispatch.py:219 call_agent() → retry loop (max 2 tentativas):
   3a. call_agent_low_level() → AgentGateway.call()
       - Monta prompt (PromptBuilder)
       - agent_client.call() → API backend (streaming, silent=True)
         → sem output visível (silent=True no gateway)
       - Retorna raw response string

   3b. resolve_agent_response() → ToolLoopService.execute()
       - Loop de tools (hops, max ~15-32)
       - Cada hop:
         a. tool_executor.maybe_execute_from_response()
         b. Se tool_result:
            - visible_text = strip_tool_block(raw)
            - Se show_output: print_response()  ← show_output=False
            - Se persist_history: persist_message() ← False
            - call_agent_fn(handoff=followup) → próximo hop
       - NENHUM feedback de progresso visível ao humano
         (sem progress_callback, sem contagem de hops, sem elapsed)

4. Se response is None:
   system_layer → "[task N] agente: sem resposta" ─── NÃO suprimida
   record_failure(agent) + can_failover? requeue ou fail

5. Se response ok:
   system_layer → "[task N] agente:\n{resultado}" ─── ADIADA se TTY ativo
   classify_task_execution_result() → ok/not ok
   Se ok: submit_for_review() ou complete_task()
   system_layer → "[task N] agente: aguardando review" ou "concluída"
     ─── AMBAS SUPRIMIDAS por _SUPPRESSED_TASK_STATUS_FRAGMENTS

Problemas acumulados:
  • silent=True → streaming invisível
  • show_output=False → resposta raw invisível
  • Sem progress_callback → humano não vê hops nem elapsed
  • Mensagens de status ("iniciando", "concluída") suprimidas
  • Resposta com resultado adiada se TTY ativo
  • Se falha: "sem resposta" sem diagnóstico (sem last_error_provider)
```

---

## 4. REVIEW SERVICE (`task_review_service.py`)

```
handler_for(agent) → review_handler(task):

1. Se executor == reviewer: → rejeita (PENDING_REVIEW, outro agente)

2. system_layer → "[task N] agente: revisando execução de X"
   ─── SUPRIMIDA (fragmento ": revisando execução de ")

3. dispatch_services.call_agent(review_prompt,
      silent=True, show_output=False) ← MESMO PROBLEMA DA EXECUÇÃO

4. classify_task_review_result() → ACEITE/REJEITA/REPLANEJA

5. Se não aceite: requeue_task_after_review()
   system_layer → "[task N] agente: review pediu x, voltou para pending"

6. Se aceite: complete_task(reviewed_by=agent)
   system_layer → "[task N] agente: review concluído"
     ─── SUPRIMIDA (fragmento ": review concluído")

Mesmos problemas: sem progress_callback, sem last_error_provider
```

---

## 5. SYSTEM LAYER — Gargalo de saída

```
SUPPRESSED (nunca aparecem):
  • ": iniciando"
  • ": aguardando review de outro agente"
  • ": concluída"
  • ": revisando task"
  • ": revisando execução de "
  • ": review concluído"
  • ": review rejeitado, aguardando outro agente"

DEFERRED (só aparecem quando Enter):
  • Mensagens com "\n" (i.e. o resultado real do agente)
  • Enfileiradas em _deferred_system_messages
  • Só exibidas via flush_deferred_messages()

Resultado final: tudo some ou atrasa.
```

---

## 6. FLUXO DE DADOS (quem chama quem)

```
core.py:730 handle_command()  ───── system_layer.py
        │                             │
        └─ task.py:219 handle_task_command()
               │
               ├─ repository.create_task() → SQLite
               │
               └─ executor thread (background) ── task_execution_service.py
                      │
                      └─ dispatch_services.call_agent() → dispatch.py
                             │
                             ├─ call_agent_low_level() → agent_gateway.py
                             │      │
                             │      └─ agent_client.call() → API llama.cpp
                             │         (streaming, silent)
                             │
                             └─ resolve_agent_response() → tool_loop.py
                                    │
                                    ├─ tool_executor.maybe_execute_from_resp()
                                    │      → executor.py
                                    │        → registry → shell/file/web/patch
                                    │
                                    └─ _call_agent_fn(handoff=followup)
                                          → dispatch.call_agent_low_level()
                                            (recursivo, sem output, sem ack)

Review segue EXATAMENTE o mesmo caminho.
```

---

## 7. 4 pontos concretos de intervenção

| # | O quê | Onde | Efeito |
|---|-------|------|--------|
| 1 | `silent=True, show_output=False` | `task_execution_service.py:112-114` e `task_review_service.py:143-145` | Streaming + resposta do agente invisíveis |
| 2 | Status suprimidos | `system_layer.py:44-52` | "iniciando", "concluída", "review" nunca aparecem |
| 3 | Mensagens adiadas | `system_layer.py:66-72` | Resultado real some no deferred queue |
| 4 | Sem `progress_callback` | `tool_loop.py:92-206` | Nenhum feedback por hop de ferramenta |

O problema não é bugs — o fluxo inteiro funciona. O problema é que **tudo que o humano deveria ver foi silenciado**.

---

## 8. Diagnóstico: infraestrutura existente mas desconectada

A infraestrutura de progresso por hop **já existe**:
- `ToolLoopService` tem suporte a `progress_callback` (tool name, hop atual, elapsed, ok/fail)
- `dispatch.call_agent` suporta `progress_callback`
- `last_error_provider` existe em `TaskExecutionService`

**Porém**:
- `TaskExecutionService` e `TaskReviewService` **não passam** `progress_callback` ao chamar `call_agent`
- `TaskReviewService` **não tem** `last_error_provider` — falhas de review mostram "sem resposta" sem diagnóstico

**Resultado**: entre "iniciando task" e "task concluída", o humano não vê nada. A infraestrutura existe mas não está conectada.