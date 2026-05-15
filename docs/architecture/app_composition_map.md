# QuimeraApp — Mapeamento de Atributos e Fronteiras de Composição

Gerado a partir de `quimera/app/core.py::QuimeraApp.__init__`.

---

## Atributos por grupo de responsabilidade

### 1. Agents

| Atributo | Tipo | Descrição |
|---|---|---|
| `selected_agents` | `list[str]` | Agentes passados via CLI |
| `active_agents` | `list[str]` | Referência para `selected_agents` (pode divergir no futuro) |
| `threads` | `int` | Número de workers paralelos |
| `agent_failures` | `defaultdict(int)` | Contador de falhas por agente |
| `_agent_failures_lock` | `Lock` | Proteção de `agent_failures` |
| `agent_client` | `AgentClient` | Gateway HTTP para chamadas de agentes |
| `summary_agent_preference` | `str` | Agente preferido para resumo de sessão |

**Dependências externas:** `AgentClient`, `workspace.cwd`, `timeout`, `visibility`, `renderer`

---

### 2. Sessão / Estado compartilhado

| Atributo | Tipo | Descrição |
|---|---|---|
| `session_state` | `dict` | Contadores e flags da sessão atual |
| `shared_state` | `dict` | Estado persistido compartilhado com agentes |
| `history` | `list` | Histórico de mensagens da sessão |
| `round_index` | `int` | Índice do round atual |
| `session_call_index` | `int` | Índice de chamadas na sessão |
| `turn_manager` | `TurnManager` | Controle de turno entre agentes |
| `_pending_input_for` | `str \| None` | Agente aguardando input humano |

**Dependências externas:** `storage.load_last_session()`, `context_manager`, `current_job_id`

---

### 3. UI / Renderização

| Atributo | Tipo | Descrição |
|---|---|---|
| `renderer` | `TerminalRenderer` | Saída formatada no terminal |
| `user_name` | `str` | Nome do usuário para exibição |
| `visibility` | `Visibility` | Nível de detalhe na renderização |
| `_nonblocking_prompt_visible` | `bool` | Prompt não-bloqueante ativo |
| `_nonblocking_prompt_text` | `str` | Texto do prompt não-bloqueante |
| `_deferred_system_messages` | `list[str]` | Mensagens pendentes de exibição |
| `_MAX_DEFERRED_SYSTEM_MESSAGES` | `int` | Limite de mensagens diferidas (20) |
| `debug_prompt_metrics` | `bool` | Flag de debug do prompt |

**Dependências externas:** `config.theme`, `config.density`

---

### 4. Input

| Atributo | Tipo | Descrição |
|---|---|---|
| `input_gate` | `InputGate` | Controle de entrada do usuário |
| `input_services` | `AppInputServices` | Serviços de input (slash commands, etc.) |
| `_nonblocking_input_thread` | `Thread \| None` | Thread de input assíncrono |
| `_nonblocking_input_queue` | `Queue \| None` | Fila de input assíncrono |
| `_nonblocking_input_status` | `str` | Estado do input assíncrono (`idle`/`reading`) |

**Dependências externas:** `renderer`, `history_file`, `_available_commands`, `_command_argument_resolver`, `system_layer`, `_output_lock`

---

### 5. Configuração

| Atributo | Tipo | Descrição |
|---|---|---|
| `config` | `ConfigManager` | Configurações persistidas do workspace |
| `auto_approve_mutations` | `bool` | Aprova mutações automaticamente |
| `execution_mode` | `str \| None` | Modo ativo: `planning`, `analysis`, etc. |
| `auto_summarize_threshold` | `int` | Rounds antes de auto-summarize |
| `idle_timeout_seconds` | `int \| None` | Timeout de inatividade |

**Dependências externas:** `workspace.config_file`

---

### 6. Workspace / Storage

| Atributo | Tipo | Descrição |
|---|---|---|
| `workspace` | `Workspace` | Estrutura de diretórios do workspace |
| `history_file` | `Path` | Arquivo de histórico readline |
| `storage` | `SessionStorage` | Persistência de sessão (JSON) |
| `tasks_db_path` | `str` | Path do banco SQLite de tasks |

**Dependências externas:** `cwd`, `workspace.logs_dir`, `workspace.tasks_db`

---

### 7. Contexto

| Atributo | Tipo | Descrição |
|---|---|---|
| `context_manager` | `ContextManager` | Contexto persistente e de sessão |

**Dependências externas:** `workspace.context_persistent`, `workspace.context_session`, `renderer`

---

### 8. Tasks

| Atributo | Tipo | Descrição |
|---|---|---|
| `task_services` | `AppTaskServices` | Criação, roteamento e execução de tasks |
| `current_job_id` | `int` | ID do job da sessão atual |
| `task_executor_factory` | `callable` | Fábrica de executores de task (`create_executor`) |

**Dependências externas:** `runtime_tasks`, `tool_executor`, `active_agents`

---

### 9. Tools

| Atributo | Tipo | Descrição |
|---|---|---|
| `tool_executor` | `ToolExecutor` | Executor de ferramentas com política de segurança |

**Dependências externas:** `task_services.build_tool_executor`, `agent_client`

---

### 10. Protocolo

| Atributo | Tipo | Descrição |
|---|---|---|
| `protocol` | `AppProtocol` | Parsing de handoff, state update, route, ack |

**Dependências externas:** `workspace.decisions_log`

---

### 11. Dispatch

| Atributo | Tipo | Descrição |
|---|---|---|
| `dispatch_services` | `AppDispatchServices` | Retry, tool loop, streaming, persistência de resposta |

**Dependências externas:** `agent_client`, `tool_executor`, `renderer`, `history`, `prompt_builder`

---

### 12. Métricas

| Atributo | Tipo | Descrição |
|---|---|---|
| `session_metrics` | `SessionMetricsService` | Métricas da sessão (latência, rounds) |
| `behavior_metrics` | `BehaviorMetricsTracker` | Rastreamento de comportamento de agentes |

**Dependências externas:** `workspace.state_dir`, `workspace.tmp.metrics_dir` (se debug)

---

### 13. Prompt

| Atributo | Tipo | Descrição |
|---|---|---|
| `prompt_builder` | `PromptBuilder` | Montagem do prompt para agentes |

**Dependências externas:** `context_manager`, `history_window`, `session_state`, `active_agents`, `behavior_metrics`

---

### 14. Chat Round

| Atributo | Tipo | Descrição |
|---|---|---|
| `chat_round_orchestrator` | `ChatRoundOrchestrator` | Orquestração de um round de conversa |

**Dependências externas:** `app` inteiro (via `self`)

---

### 15. Summarizer

| Atributo | Tipo | Descrição |
|---|---|---|
| `session_summarizer` | `SessionSummarizer` | Resumo automático de sessões longas |

**Dependências externas:** `agent_client`, `active_agents`

---

### 16. Camada de sistema

| Atributo | Tipo | Descrição |
|---|---|---|
| `system_layer` | `AppSystemLayer` | Exibição de mensagens de sistema, notificações |

**Dependências externas:** `app` inteiro (via `self`)

---

### 17. Locks de concorrência

| Atributo | Tipo | Descrição |
|---|---|---|
| `_lock` | `Lock` | Lock principal da aplicação |
| `_output_lock` | `Lock` | Serialização de output |
| `_counter_lock` | `Lock` | Proteção de contadores |

---

### 18. Serviços de sessão

| Atributo | Tipo | Descrição |
|---|---|---|
| `session_services` | `AppSessionServices` | Serviços de ciclo de sessão (resumo, save, restore) |

**Dependências externas:** `app` inteiro (via `self`)

---

## Grupos candidatos a objetos de composição explícitos

| Grupo proposto | Atributos-chave | Dependências de entrada |
|---|---|---|
| `AgentHub` | `active_agents`, `agent_failures`, `agent_client`, `threads`, `summary_agent_preference` | `cwd`, `timeout`, `visibility`, `renderer` |
| `SessionContext` | `session_state`, `shared_state`, `history`, `round_index`, `session_call_index`, `turn_manager` | `storage`, `context_manager`, `current_job_id` |
| `UILayer` | `renderer`, `visibility`, `user_name`, prompt não-bloqueante, mensagens diferidas | `config.theme`, `config.density` |
| `InputController` | `input_gate`, `input_services`, threads não-bloqueantes | `renderer`, `history_file`, resolvers, `system_layer`, `_output_lock` |
| `AppConfig` | `config`, `auto_approve_mutations`, `execution_mode`, `auto_summarize_threshold`, `idle_timeout_seconds` | `workspace.config_file`, parâmetros CLI |
| `WorkspaceIO` | `workspace`, `history_file`, `storage`, `tasks_db_path` | `cwd` |
| `MetricsBundle` | `session_metrics`, `behavior_metrics` | `workspace.state_dir` |
| `PromptAssembly` | `context_manager`, `prompt_builder` | `workspace`, `renderer`, `active_agents`, `behavior_metrics` |

### Grupos que ainda não têm fronteira clara

- `system_layer` e `chat_round_orchestrator` ainda recebem `app` inteiro — precisam de refatoração própria (seções 5 e 6 do TODO).
- `task_services` e `dispatch_services` também recebem `app` — são alvo das seções 2 e 6 do TODO.
- `protocol` recebe `app` — alvo da seção 7.
- `session_services` (`AppSessionServices(app)` — `core.py:85`) recebe `app` inteiro — acoplado ao mesmo nível que os demais serviços; fronteira a definir em seção futura.
- `input_services` (`AppInputServices(app, input_resolver)` — `core.py:93`) recebe `app` inteiro e `input_resolver`; acessa `system_layer` e `_output_lock` diretamente; fronteira a definir em seção futura.

---

## Regra de contorno (seção 1, última tarefa)

> Não adicionar novo atributo em `QuimeraApp.__init__` sem justificar em qual grupo ele pertence e por que não pode ser encapsulado diretamente no serviço responsável.

Se um atributo novo for necessário, ele deve ser documentado aqui com: grupo, dependências e razão pela qual não pode ficar no serviço correspondente.
