# Quimera — Arquitetura Atual

> Documento que descreve o estado real da arquitetura do Quimera, incluindo estrutura de módulos, dependências, modelo de threading e dívida técnica conhecida.

---

## 1. Visão Geral

O Quimera é um orquestrador multiagente terminal-based que permite aos usuários interagirem com diversos agentes de IA (Claude, Codex, Gemini, Ollama, OpenCode, etc.) através de uma interface unificada. O sistema executa tarefas em paralelo, gerencia estado de sessão, fornece uma interface rica com suporte a markup, temas e auditoria, e oferece um runtime de execução de tools em ambiente sandboxed.

A arquitetura é organizada em torno de um loop principal de eventos em `quimera/app/core.py`, com separação crescente de responsabilidades entre camadas. O desmonte incremental do monólito original extraiu módulos como `runtime_state`, `session_bootstrap`, `tty_control`, `toolbar`, `bug_services`, `command_router`, `chat_processor` e `ui_event_handler`. As violações de fronteira documentadas anteriormente foram resolvidas (seções 7.3 e 7.4).

---

## 2. Estrutura de Diretórios

```
quimera/
├── app/                              # Camada de aplicação (orquestração principal)
│   ├── core.py                       # Loop principal, estado e coordenação (~1611 linhas)
│   ├── runtime_state.py              # AppRuntimeState: estado de runtime (input, chat, slots)
│   ├── session_bootstrap.py          # Bootstrap/inicialização da sessão (paths, debug, bugs)
│   ├── session_paths.py              # Resolução de paths de sessão (logs, bugs, debug)
│   ├── tty_control.py                # Controle de TTY (suspend/resume do renderer)
│   ├── toolbar.py                    # ToolbarManager: toolbar dinâmica do prompt_toolkit
│   ├── toolbar_coordinator.py        # Coordenação entre toolbars (prompt_toolkit ↔ Textual)
│   ├── bug_services.py               # BugServices: detecção e correlação de bugs de runtime
│   ├── command_router.py             # Roteamento de comandos slash (/task, /help, etc.)
│   ├── chat_processor.py             # ChatProcessor: processamento de uma rodada de chat
│   ├── chat_lifecycle.py              # Ciclo de vida de chat (início, transições, fim)
│   ├── chat_round.py                 # Lógica de uma rodada de chat (humano → agente → resultado)
│   ├── ui_event_handler.py            # UIEventHandler: processamento de eventos de UI
│   ├── dispatch.py                   # Despacho de chamadas para agentes e tools
│   ├── agent_call_service.py         # Serviço de chamada a agentes (retry, timeout)
│   ├── agent_gateway.py              # Interface de baixo nível para AgentClient
│   ├── agent_pool.py                 # Gerenciamento de pool de agentes disponíveis
│   ├── agent_run_events.py           # Eventos de execução de agentes
│   ├── agent_failure_tracker.py      # Rastreamento de falhas de agentes
│   ├── worker.py                     # Worker thread de chat (isola exceções, recupera turno)
│   ├── protocol.py                   # Parsing/geração do protocolo de delegation entre agentes
│   ├── event_sink.py                 # Sistema de publish-subscribe de eventos internos
│   ├── render_event.py               # Definição de eventos de renderização
│   ├── handlers.py                   # Handlers de eventos de UI
│   ├── display_service.py            # Serviço auxiliar de renderização (formatos, estilos)
│   ├── system_layer.py               # Processamento de comandos de sistema (/task, /help, ...)
│   ├── session.py                    # Gerenciamento de histórico e recuperação de sessão
│   ├── session_metrics.py            # Métricas de sessão (turnos, latências, etc.)
│   ├── turn.py                       # Gerenciamento de turno (humano ↔ agente)
│   ├── staging.py                    # Gerenciamento de estado de staging/pending
│   ├── inputs.py                     # Integração com InputGate
│   ├── simple_input_gate.py          # SimpleInputGate para modo pipe/non-TTY
│   ├── prompt_formatter.py           # Formatação do prompt visível ao humano
│   ├── welcome_presenter.py          # Tela de boas-vindas da sessão
│   ├── completion_dropdown.py        # Dropdown de autocomplete do input
│   ├── interfaces.py                 # Protocolos/interfaces entre camadas
│   └── config.py                     # Configuração de nível de aplicação
│
├── tasks/                            # Sistema de tasks (extraído para módulo próprio)
│   ├── __init__.py                   # Ponto de entrada do módulo de tasks
│   ├── api.py                        # API pública de criação e gerenciamento de tasks
│   ├── services.py                   # Serviços de orquestração de tasks
│   ├── router.py                     # Roteamento de tasks para o agente adequado
│   ├── classifiers.py                # Classificação de resultados (ACCEPT/RETRY/REPLAN/REJECT)
│   ├── execution.py                  # Execução de tasks com pool de workers
│   ├── executor.py                   # Pool de execução de tasks
│   ├── review.py                     # Serviço de revisão de tasks
│   ├── reviewer.py                   # Revisão de resultados por agente
│   ├── failover.py                   # Políticas de failover e retry de tasks
│   ├── repository.py                 # Acesso ao banco de dados de tasks (SQLite)
│   ├── prompt.py                     # Criação de prompts específicos para tasks
│   ├── utils.py                      # Utilitários de suporte a tasks
│   ├── planner.py                    # Planejamento de tasks (em desenvolvimento)
│   ├── events.py                     # Definição de eventos do ciclo de vida de tasks
│   └── runner.py                     # Runner de tasks individuais
│
├── runtime/                          # Executor de tools e runtime de agentes
│   ├── executor.py                   # Executor genérico de operações do runtime
│   ├── streaming.py                  # Suporte a streaming de respostas de agentes
│   ├── registry.py                   # Registro de tools disponíveis no runtime
│   ├── tool_hops.py                  # Encadeamento de chamadas de tools (multi-hop)
│   ├── input_broker.py               # Mediação de input entre runtime e agentes
│   ├── tool_preview.py               # Pré-visualização de resultados de tools
│   ├── process_supervisor.py         # Supervisão de processos do runtime
│   ├── workspace_policy.py           # Políticas de workspace (permissões, paths)
│   ├── approval_broker.py            # Broker de aprovações entre runtime e usuário
│   ├── approval.py                   # Aprovação interativa de ações do runtime
│   ├── policy.py                     # Políticas de execução (permissões, sandboxing)
│   ├── models.py                     # Modelos de dados do runtime (Task, Result, etc.)
│   ├── errors.py                     # Exceções específicas do runtime
│   ├── config.py                     # Configuração do runtime (timeouts, limites)
│   ├── mcp/                          # Servidor MCP (Model Context Protocol)
│   │   ├── server.py                 # Servidor JSON-RPC 2.0 sobre stdio/socket/HTTP
│   │   ├── session.py                # Gerenciamento de sessão MCP
│   │   ├── http_server.py            # Servidor HTTP para MCP
│   │   └── __main__.py               # Entrypoint standalone do servidor MCP
│   ├── drivers/                      # Drivers de execução de agentes
│   │   ├── repl.py                   # Driver REPL (processo interativo persistente)
│   │   ├── openai_compat.py          # Driver compatível com API OpenAI
│   │   ├── prompt_adapter.py         # Adaptador de prompts para drivers
│   │   └── tool_schemas.py           # Schemas JSON de tools para drivers de API
│   └── tools/                        # Implementações de tools individuais
│       ├── shell.py                  # Tool de execução shell
│       ├── files.py                  # Tool de acesso ao filesystem
│       ├── web.py                    # Tool de acesso à web
│       ├── patch.py                  # Tool de aplicação de patches
│       ├── delegate.py               # Tool de delegação cross-MCP entre agentes
│       ├── base.py                   # Classe base para tools
│       ├── git.py                    # Tool de operações git
│       ├── browser/                  # Tools de automação de navegador (browser_*)
│       ├── mcp_clients.py            # Clientes MCP auxiliares das tools
│       ├── memory.py                 # Tool de acesso à memória do workspace
│       ├── state.py                  # Tool de acesso ao shared state
│       ├── todo.py                   # Tool de gerenciamento de TODOs
│       ├── interaction.py            # Tool de interação com o usuário
│       ├── tasks.py                  # Tool de criação/consulta de tasks
│       └── _helpers.py               # Helpers internos das tools
│
├── agents/                           # Infraestrutura de comunicação com agentes LLM
│   ├── client.py                     # Cliente unificado para agentes (CLI, API, profile)
│   ├── parsers.py                    # Parsers de saída de agentes (JSON, markdown, etc.)
│   ├── process_runner.py             # Execução e gerenciamento de subprocessos de agentes
│   ├── signal_guard.py               # Proteção contra sinais durante chamadas de agentes
│   ├── text_filters.py             # Filtros de texto (strip de ruído, normalização)
│   └── warm_pool.py                  # Pool de processos pré-aquecidos de agentes
│
├── profiles/                         # Sistema de profiles por agente
│   ├── base.py                       # Registro central (ExecutionProfile, _profile_registry)
│   ├── claude.py                     # Profile para Claude (Anthropic CLI)
│   ├── codex.py                      # Profile para Codex (OpenAI CLI)
│   ├── gemini.py                     # Profile para Gemini (Google CLI)
│   ├── ollama.py                     # Profile para Ollama (LLMs locais)
│   ├── opencode.py                   # Profile para OpenCode (vários backends)
│   ├── antigravity.py                # Profile para Antigravity (agente de refatoração)
│   ├── fake.py                       # Profile fake para testes/integrado
│   ├── mock.py                       # Profile mock para testes
│   └── spy_utils.py                  # Utilitários de espionagem/observação de profiles
│
├── ui/                               # Camada de apresentação
│   ├── renderer.py                   # TerminalRenderer (Rich + Rich.Live, ~1200 linhas)
│   ├── audit.py                       # Logger de auditoria de eventos de renderização
│   ├── compositor.py                  # Compositor de layouts de UI
│   ├── overlay.py                    # Gerenciamento de sobreposições na tela
│   ├── text.py                        # Utilitários de texto para UI
│   ├── events.py                     # Eventos da camada de UI
│   ├── window_manager.py             # Gerenciamento de janelas
│   ├── windows.py                    # Definição de janelas da UI
│   ├── agent_window_controller.py    # Controle de janelas de agentes
│   └── textual/                      # Interface Textual (TUI moderna)
│       ├── app.py                    # Aplicação Textual principal
│       ├── bridge.py                 # Ponte entre o loop legacy e Textual
│       ├── input_gate.py             # InputGate baseado em Textual
│       ├── renderer.py              # Renderizador Textual
│       ├── renderables.py            # Componentes renderizáveis Textual
│       ├── widgets.py                # Widgets customizados
│       ├── feed_model.py             # Modelo do feed de mensagens
│       ├── styles.py                 # Estilos e temas Textual
│       ├── constants.py              # Constantes da UI Textual
│       ├── events.py                 # Eventos específicos Textual
│       ├── direct_input.py           # Modo de input direto
│       └── terminal_modes.py         # Gerenciamento de modos de terminal
│
├── domain/                           # Modelos de domínio (independentes de framework)
│   └── session_state.py              # SessionState thread-safe (RLock), estado da sessão
│
├── evidence/                         # Sistema de evidências (rastreamento de contexto)
│   ├── models.py                       # Modelos de evidência (EvidenceItem, etc.)
│   ├── store.py                      # Armazenamento e recuperação de evidências
│   ├── parser.py                     # Parser de blocos de evidência no texto
│   └── formatter.py                  # Formatação de evidências para o prompt
│
├── sandbox/                          # Isolamento de execução
│   └── bwrap.py                      # Sandbox via bubblewrap (bwrap), paths RW/RO
│
├── devtools/                         # Ferramentas de desenvolvimento
│   ├── fake_agents.py                 # Agentes falsos para testes de integração
│   └── __init__.py
│
├── prompt.py                         # Injeção do bloco de execução no prompt (goal-driven)
├── prompt_budget.py                  # Orçamento de tokens: trunca seções por prioridade
├── prompt_templates.py               # Templates de prompt (seções, marcadores)
├── prompt_kinds.py                   # Tipos de prompt (chat, task, review, etc.)
├── context.py                        # Montagem dinâmica do contexto para prompts
├── shared_state.py                   # Dicionário de estado compartilhado entre agentes
├── storage.py                        # Persistência de sessão (logs, histórico em arquivo)
├── workspace.py                      # Representação do workspace do usuário
├── workspace_memory.py               # Memória persistente do workspace
├── session_summary.py                 # Sumarização de sessão para manter contexto compacto
├── bugs.py                           # Detecção, correlação e reporte de bugs de runtime
├── agent_events.py                   # Definição de eventos de agentes (AgentEvent, etc.)
├── delegate_presenter.py             # Formatação de steps para exibição
├── shared_state_presenter.py         # Formatação do shared state para exibição
├── execution_mode_presenter.py       # Formatação do modo de execução ativo
├── spy_output_presenter.py           # Formatação de saída de spy (debug de agentes)
├── memory_selector.py                # Seleção de memória relevante para o prompt
├── themes.py                         # Definição e carregamento de temas visuais
├── config.py                         # Configuração global do projeto
├── constants.py                      # Constantes (prompts de sistema, comandos, prefixos)
├── modes.py                          # Modos de operação (debug, produção, etc.)
├── metrics.py                        # Rastreamento de métricas de comportamento
├── env_config.py                      # Carregamento de variáveis de ambiente
├── paths.py                           # Resolução de paths (workspace, dados, config)
├── cli.py                             # Ponto de entrada CLI (argparse, bootstrap)
├── editor.py                          # Integração com editor externo
├── clipboard_support.py               # Suporte a clipboard
├── connection_configurator.py         # Configuração de conexões de agentes
└── process_factory.py                 # Fábrica de processos para workers
```

---

## 3. Camadas e Responsabilidades

### 3.1 Camada de Aplicação (`app/`)

- **`core.py`**: Orquestrador central. Loop principal, estado da aplicação, coordenação de componentes, despacho para agentes e tasks. Reduzido de ~2300 para ~1611 linhas após extração de módulos especializados (ver abaixo).
- **`runtime_state.py`** (`AppRuntimeState`): Container de estado de runtime: status de input não-bloqueante, contadores de chat, semáforo de slots, executor de threads. Substitui o `_BACKWARD_MAP` legado que redirecionava atributos privados.
- **`session_bootstrap.py`**: Inicialização da sessão: resolução de paths (logs, bugs, debug), análise de bugs de sessão anterior. Extraído de `core.py` para isolar lógica de startup.
- **`tty_control.py`**: Controle de TTY: suspend e resume do renderer durante operações bloqueantes (editor, aprovação interativa). Coordena `TerminalRenderer` + `InputGate`.
- **`toolbar.py`** (`ToolbarManager`): Geração e atualização da toolbar dinâmica. Renderiza estado atual (agentes, turno, tema, métricas).
- **`bug_services.py`** (`BugServices`): Detecção, correlação e reporte de bugs de runtime (burst de falhas, timeouts). Extraído de `AppBugServices` via rename + flatten.
- **`command_router.py`**: Roteamento de comandos slash internos. Mantém o mapeamento de `/cmd` → handler sem exposição direta ao `core.py`.
- **`chat_processor.py`** (`ChatProcessor`): Processamento de uma rodada de chat completa: input → agente → resultado → update de estado. Orquestra workers e sincronização.
- **`ui_event_handler.py`** (`UIEventHandler`): Processamento de eventos da fila de UI (`ui_event_queue`). Usa `InputGate.is_active()` como fonte primária para decidir se o prompt deve ser redesenhado.
- **`chat_round.py`**: Encapsula a lógica de uma rodada de chat completa (leitura de input → chamada ao agente → processamento de resultado → update de estado).
- **`dispatch.py`**: Chama agentes via `AgentClient` e executa tools via `ToolLoop`. Gerencia o ciclo de vida de chamadas e resultados.
- O módulo `tasks/` (em `quimera/tasks/`) implementa o ciclo de vida completo de tasks: criação, classificação, atribuição, execução, revisão, failover e notificação.
- **`ui/textual/input_gate.py`** (`TextualInputGate`): Gate de input baseado em Textual para a TUI. Usa fila thread-safe para receber input do widget `Input` do Textual. **`ui/textual/renderer.py`** (`TextualRenderer`) emite eventos para o `TextualUiBridge`.
- **`simple_input_gate.py`** (`SimpleInputGate`): Gate de input para modo pipe/sem TTY, usando `input()` padrão. Mantém a mesma interface pública de `TextualInputGate`.
- **`prompt_formatter.py`** (`PromptFormatter`): Formata o prompt visível ao humano com nome e modo atual.
- **`inputs.py`**: Integração de alto nível com `InputGate`, exposta ao `core.py`.
- **`event_sink.py`**: Publish-subscribe interno. Eventos publicados de worker threads são enfileirados na `ui_event_queue`; publicados da main thread são processados diretamente.
- **`system_layer.py`**: Processa comandos `/cmd` do usuário. Adaptadores legados (`_LegacyProfileResolver`, `_LegacyAgentPoolAdapter`) mantidos para compatibilidade de migração.
- **`protocol.py`**: Define e parseia o formato de delegation entre agentes (`type`, `route`, `content`, `metadata`).
- **`interfaces.py`**: Protocolos (typing) que tentam estabelecer contratos entre camadas — ainda subutilizados.

### 3.2 Camada de Runtime (`runtime/`)

Executor de tools e agentes em ambiente potencialmente sandboxed.

- **`drivers/`**: Drivers de execução de agentes. `repl.py` gerencia processos interativos persistentes; `openai_compat.py` suporta a API OpenAI; `tool_schemas.py` define schemas JSON para uso em API mode.
- **`tools/`**: Implementações individuais de tools: shell, filesystem, web, patch, tasks.
- **`task_planning.py`**: Decompõe goals em tasks individuais.
- **`approval.py`**: Solicita aprovação interativa do usuário antes de ações potencialmente destrutivas.
- **`policy.py`**: Define o que pode ou não ser executado (permissões de sandbox).

### 3.3 Camada de Agentes (`agents/`)

- **`client.py`**: Interface unificada para todos os backends (CLI local, API remota, profile). Suporta streaming. `run()` não é reentrante: execução concorrente sobre o mesmo client é detectada e logada como erro; delegações usam `AgentClient` isolado via dispatch de background. `add_cancel_listener()` permite propagar o cancelamento do usuário a clients de background.
- **`warm_pool.py`**: Pool de processos pré-iniciados para reduzir latência de cold start.
- **`process_runner.py`**: Gerencia subprocessos de agentes (stdin/stdout/stderr, timeout, sinalização).

### 3.4 Camada de Profiles (`profiles/`)

- **`base.py`**: Define `ExecutionProfile` (dataclass com metadados: `name`, `driver`, `supports_task_execution`, `runtime_rw_paths`, etc.) e `_profile_registry`.
- Profiles concretos (`claude.py`, `codex.py`, `gemini.py`, `ollama.py`, `opencode.py`): cada um registra um `ExecutionProfile` com configuração específica do backend.
- `mock.py`: Profile para testes unitários.

### 3.5 Camada de Apresentação (`ui/`)

- **`renderer.py`** (`TerminalRenderer`): Renderizador terminal usando `Rich` e `Rich.Live`. Gerencia buffer de streaming, temas, scrollback e atualizações em tempo real. Deve ser acessado **apenas** da main thread.
- **`audit.py`**: Registra eventos de renderização para depuração.

### 3.6 Domínio (`domain/`)

- **`session_state.py`** (`SessionState`): Container thread-safe (`threading.RLock`) para o estado corrente da sessão. Compartilhado entre main thread e workers.

### 3.7 Evidências (`evidence/`)

Sistema de rastreamento de contexto verificável. Evidências são extraídas das respostas dos agentes, armazenadas em `store.py` e injetadas no prompt via `formatter.py` para fornecer contexto verificável nas próximas rodadas.

### 3.8 Sandbox (`sandbox/`)

- **`bwrap.py`**: Integração com `bubblewrap` (bwrap) para isolamento de processos. Define paths de leitura/escrita permitidos por agente via `ExecutionProfile.runtime_rw_paths`.

### 3.9 Sistema MCP (Model Context Protocol)

O Quimera implementa o protocolo MCP (`2025-11-25`, com negociação para versões anteriores) para expor as ferramentas do runtime a agentes compatíveis. O servidor MCP é iniciado por sessão em um socket Unix com autenticação por token.

#### 3.9.1 Componentes

| Módulo | Arquivo | Responsabilidade |
|---|---|---|
| **MCPServer** | `runtime/mcp/server.py` | Servidor JSON-RPC 2.0 sobre stdio/socket/HTTP. Métodos principais: lifecycle, `ping`, tools, resources, prompts, completion e logging |
| **ToolExecutor** | `runtime/executor.py` | Executa tools com validação de política, aprovação e resolução de aliases |
| **ToolRegistry** | `runtime/registry.py` | Registro nome → handler (dict simples) |
| **DelegateTools** | `runtime/tools/delegate.py` | Implementa `delegate` — delegação cross-MCP entre agentes |
| **Proxy stdio→socket** | `runtime/mcp/server.py:_proxy_stdio_to_socket` | Ponte transparente entre stdio do agente e socket Unix do servidor |
| **Profile MCP injection** | `profiles/{claude,codex,opencode}.py` | Cada profile injeta config MCP no formato nativo do agente |
| **Tool schemas** | `runtime/drivers/tool_schemas.py` | Fonte única de schemas: `resolve_tool_schemas()` filtra por registro/política |
| **Prompt conditionals** | `prompt.md`, `task_prompt.md` | Blocos `<!-- IF:mcp_enabled -->` ativam instruções MCP nos prompts |
| **Config bridge** | `app/core.py:configure_mcp_socket()` / `configure_mcp_http()` | Propaga socket/http endpoint e token para todos os profiles ativos |

#### 3.9.2 Fluxo de Inicialização

```
CLI (socket padrão)
  ├── Usa token de `--mcp-token-env`/`QUIMERA_MCP_TOKEN` quando definido; caso contrário gera token com secrets.token_urlsafe(32)
  ├── Socket padrão: workspace.tmp.root / f"mcp-{rand}.sock"; `--mcp-socket [path]` permite selecionar/definir path
  ├── HTTP opcional: `--mcp-http --mcp-host 127.0.0.1 --mcp-port 9090` expõe `/mcp`; `--mcp-token-env` permite token fixo para clientes remotos
  ├── Cria MCPServer(tool_executor, auth_token=mcp_token)
  ├── inicia socket ou MCP_HTTPServer em background
  ├── app.configure_mcp_socket(...) ou app.configure_mcp_http(...) → propaga para profiles
  └── session_state["mcp_enabled"] = True      → ativa blocos no prompt
```

#### 3.9.3 Injeção por Profile

| Profile | Formato | Exemplo |
|---|---|---|
| **Codex** | `-c mcp_servers.quimera.command=python -c mcp_servers.quimera.args=[...]` | Argumentos CLI no estilo TOML |
| **Claude** | `--mcp-config {"mcpServers":{"quimera":{"type":"stdio","command":"python","args":[...]}}}` | JSON injetado como argumento |
| **OpenCode** | `OPENCODE_CONFIG_CONTENT={"mcp":{"quimera":{"type":"local","command":[...],"enabled":true}}}` | Variável de ambiente |
| **Gemini** | Sem suporte a MCP | — |

#### 3.9.4 Autenticação

Cada conexão socket envia uma linha JSON como primeiro frame:
```json
{"quimera_auth_token": "<token-sessao>"}
```
O servidor valida com timeout de 5s. Token inválido → conexão fechada.

#### 3.9.5 Cross-MCP (delegate)

A ferramenta `delegate` é o mecanismo central de interoperabilidade entre agentes no pool:

- **Disponibilidade**: verifica se `_delegate_fn` foi injetado pelo `ToolExecutor.set_delegate_fn()`.
- **Parâmetros**: `target_agent` (obrigatório), `request` (obrigatório), `context`, `fallback_agents`, `steps`.
- **Validação**: agente alvo deve estar no pool ativo. `steps` suporta cadeias multi-passo.
- **Execução isolada**: toda delegação originada de tool call (socket interno ou HTTP) roda em serviços de dispatch de background com um `AgentClient` isolado criado por chamada (`wiring.py:_make_background_delegate_fn`). O `run()` do `AgentClient` não é reentrante — o agente delegado nunca executa sobre o client cujo `run()` do agente origem ainda está ativo.
- **Herança de runtime**: o client de background herda `pause_idle_if` e `process_supervisor` do client do chat — um delegado em silêncio aguardando tool longa não morre por idle timeout, e seus subprocessos entram no `terminate_all()` de shutdown/cancelamento.
- **Cancelamento**: ESC/Ctrl+C no fluxo principal propaga aos clients de background vivos via `AgentClient.add_cancel_listener()` → `TaskExecutorPool.cancel_background_work()`.
- **Truncamento**: task limitada a 1200 caracteres, contexto a 4000.

#### 3.9.6 MCP-First Mode

Agentes devem usar exclusivamente a tool `delegate` via MCP. Envelopes textuais legados foram removidos.

#### 3.9.7 Ferramentas Expostas via tools/list

Tools definidas em `TOOL_SCHEMAS`, filtradas por:
1. Registro no executor (intersecção com handlers registrados)
2. Configuração (tools de task ocultas sem db_path)
3. Política (tools bloqueadas removidas)
4. Disponibilidade de `delegate` (oculta se fn não injetada)

### 3.10 Módulos Raiz de Prompt

| Módulo | Responsabilidade |
|---|---|
| `prompt.py` | Injeta bloco de execução goal-driven no topo do prompt quando `goal_canonical` está ativo |
| `prompt_budget.py` | Gerencia orçamento de tokens: trunca seções por prioridade quando necessário |
| `prompt_templates.py` | Templates base: seções obrigatórias/opcionais, marcadores XML |
| `prompt_kinds.py` | Tipos de prompt por contexto (chat, task, review, delegation) |
| `context.py` | Monta o contexto dinâmico: histórico, shared state, instruções |

### 3.11 Outros Módulos Raiz

| Módulo | Responsabilidade |
|---|---|
| `shared_state.py` | Dicionário de estado compartilhado entre agentes durante execução |
| `storage.py` | Persistência de sessão em arquivo (logs, histórico) |
| `workspace.py` | Representação do workspace: arquivos relevantes, CWD do projeto |
| `session_summary.py` | Sumariza histórico longo para caber no contexto dos agentes |
| `bugs.py` | Detecta, correlaciona e reporta bugs de runtime (burst de falhas, timeouts) |
| `themes.py` | Carrega e aplica temas visuais ao renderer e toolbar |
| `constants.py` | Constantes de sistema: prompts goal-driven, regras de revisão, comandos |
| `paths.py` | Resolve paths de dados (`~/.local/share/quimera/...`), config e workspace |
| `agent_events.py` | Tipos de eventos emitidos pelos agentes durante execução |
| `*_presenter.py` | Formatadores de seções específicas do prompt (delegation, shared state, etc.) |

---

## 4. Modelo de Threading e Concorrência

### 4.1 Main Thread (UI Thread)

- Executa o loop principal (`core.py:run()`).
- Lê input do usuário via `InputGate` (`TextualInputGate` na TUI, `SimpleInputGate` em modo pipe).
- Drena a `ui_event_queue` e atualiza o `TerminalRenderer` — acesso exclusivo.
- Gerencia o `TurnManager` (alternância humano ↔ agente).
- Processa comandos de sistema via `AppSystemLayer`.

### 4.1.5 MCP Server Thread

- O `MCPServer` roda em uma thread daemon (iniciada via `start_background()` em `cli.py`).
- Aceita conexões socket Unix concorrentes.
- Cada conexão executa o loop JSON-RPC na thread do `accept()`.
- O `ToolExecutor` (compartilhado com a main thread) é chamado dentro dessas threads — o executor é thread-safe via `threading.Lock`.

### 4.2 Worker Threads

- Usados para operações bloqueantes:
  - Chamadas a agentes de IA (`AgentClient`).
  - Execução de tools de runtime (shell, web, filesystem).
  - Execução e revisão de tasks.
- Principais implementações:
  - `ChatWorker` (`app/worker.py`): workers de chat.
  - `tasks/execution.py` (`TaskExecutionService`): pool para tasks.
- Comunicam com a main thread apenas via:
  - `ui_event_queue` (`queue.Queue`): eventos de renderização.
  - `EventSink`: callbacks registrados da main thread.

### 4.3 Sincronização

| Mecanismo | Localização | Uso |
|---|---|---|
| `ui_event_queue` | `core.py` | Worker → main thread (eventos de UI) |
| `EventSink` | `app/event_sink.py` | Publish-subscribe interno |
| `TurnManager` | `app/turn.py` | Alternância de turno com lock |
| `SessionState` | `domain/session_state.py` | Estado compartilhado com `threading.RLock` |
| `InputGate.is_active()` | `ui/textual/input_gate.py` / `app/simple_input_gate.py` | Árbitro primário de estado de prompt ativo |
| `AppRuntimeState` | `app/runtime_state.py` | Estado de runtime (slots, contadores, semáforo) — sem mais atributos privados via `_BACKWARD_MAP` |

### 4.4 Problemas Conhecidos de Threading

- `TerminalRenderer` não é thread-safe. Apesar da `ui_event_queue`, alguns caminhos em `display_service.py` e `agent_gateway.py` ainda chamam métodos do renderer diretamente de worker threads.
- O `TextualRenderer` não gerencia overlay ou cursor positioning — eventos são emitidos para o bridge Textual que gerencia o ciclo de renderização.
- A toolbar do Textual (`#toolbar` Static) só é atualizada via eventos `prompt` do bridge.

---

## 5. Fluxo de Controle Principal

O loop em `quimera/app/core.py:run()` segue este fluxo:

1. **Inicialização**: carrega configuração, profiles, estado de sessão; cria renderer, InputGate, TurnManager, pool de workers, `ui_event_queue`. O servidor MCP é iniciado em `cli.py` antes de `app.run()` — resolve token de autenticação por env ou gera token aleatório, cria socket Unix, inicia MCPServer em background e propaga configuração para todos os profiles ativos.
2. **Loop principal**:
   - Drena `ui_event_queue` → atualiza renderer.
   - Se não for turno do humano: aguarda com timeout (verifica worker vivo).
   - Lê input via `InputGate`.
   - Se comando `/cmd`: processa via `AppSystemLayer`.
   - Se mensagem: submete ao worker, avança turno.
   - Exceções no loop: capturadas, logadas, turno retorna ao humano.
3. **Encerramento**: graceful shutdown de workers, salva sessão e métricas.

---

## 6. Pontos de Integração e Dependências Chave

### 6.1 Integração com Agentes

- `agents/client.py` abstrai todos os backends: CLI local (`claude`, `codex`, `gemini`), API (OpenAI-compat), driver REPL persistente.
- `profiles/` define metadados por agente: capacidades, paths de sandbox, driver, tipos de task suportados, e mecanismo de injeção MCP via `mcp_server_args()`.
- `agents/warm_pool.py` mantém processos pré-aquecidos para reduzir latência.
- `runtime/mcp/server.py` expõe as ferramentas do runtime via protocolo MCP; o driver OpenAI-compatible também usa tool calling nativo quando disponível, e ambos convergem para `ToolExecutor.execute(ToolCall(...))`.
- `profiles/{claude,codex,opencode}.py` cada um implementa `mcp_server_args(socket_path)` para injetar a configuração MCP no formato nativo do agente (JSON, CLI args, env vars).

### 6.2 Sistema de Tasks

O sistema de tasks foi extraído para o módulo `quimera/tasks/`, removendo a dispersão anterior entre `app/task*.py` e `runtime/task*.py`.

Fluxo: `/task` → `tasks/services.py` (orquestração) → `tasks/repository.py` (persistência) → `tasks/classifiers.py` (tipo) → `tasks/router.py` (agente) → `tasks/execution.py` (execução em worker) → `tasks/review.py` (revisão opcional) → `tasks/failover.py` (retry/reject) → notificação via `ui_event_queue`.

### 6.3 Construção de Prompt

O prompt final é montado com estas seções em ordem de prioridade:

1. `<rules>`: regras de conduta e goal-driven execution (de `constants.py` + `prompt.py`).
2. `<execution_state>`: estado de execução atual (goal, step, criteria) — injetado por `prompt.py`.
3. `<shared_state>`: estado compartilhado entre agentes (`shared_state_presenter.py`).
4. `<evidence>`: evidências acumuladas (`evidence/formatter.py`).
5. `<recent_conversation>`: últimas trocas de mensagens.
6. `<persistent_context>`: resumo acumulado de sessão (`session_summary.py`).
7. `<current_turn>`: mensagem atual.

Orçamento de tokens gerenciado por `prompt_budget.py`: trunca seções de menor prioridade primeiro.

### 6.4 Protocolo de Delegation entre Agentes

O Quimera suporta dois mecanismos de delegation:

#### 6.4.1 Delegation via MCP (delegate) — Preferencial

Quando o MCP está ativo (padrão), os agentes usam a tool `delegate` exposta via protocolo MCP. Definido em `runtime/tools/delegate.py`:

```json
{
  "name": "delegate",
  "arguments": {
    "target_agent": "codex",
    "request": "Implementar função de leitura",
    "context": "contexto opcional",
    "fallback_agents": ["claude"],
    "steps": [{"target_agent": "claude", "request": "Revise o resultado"}]
  }
}
```

- **Disponibilidade**: depende de `_delegate_fn` injetado pelo app (`ToolExecutor.set_delegate_fn()`).
- **Validação**: o alvo é verificado contra `_resolve_active_agents()` no pool.
- **Failover**: `fallback_agents` tentado em sequência se o primário falhar.
- **Cadeias**: `steps` executa passos adicionais, cada um com seu próprio fallback.
- **Isolamento**: a execução usa dispatch de background com `AgentClient` isolado por chamada (cancel_event próprio), evitando reentrância do client principal; o cancelamento do usuário propaga aos clients de background (ver §3.9.5).


### 6.5 Arquitetura Orientada a Goals

`constants.py` define os prompts: `PROMPT_GOAL_LOCK`, `PROMPT_STEP_LOCK`, `PROMPT_ACCEPTANCE_CRITERIA`, `PROMPT_REVIEWER_RULE` (ACCEPT/RETRY/REPLAN/REJECT). `prompt.py` injeta o bloco de execução quando `shared_state` contém `goal_canonical`.

---

## 7. Dívida Técnica e Problemas Conhecidos

### 7.1 Acoplamento e Responsabilidades Sobrepostas

- `core.py` (~1611 linhas, reduzido de ~2300): loop de I/O, gerenciamento de estado, dispatch, tasks. Decomposição em andamento — 8 módulos extraídos até o momento.
- Muitos serviços em `app/` recebem a instância inteira de `app` e acessam atributos diretamente, em vez de dependências injetadas.
- `interfaces.py` define Protocolos mas poucos componentes os usam; a maioria depende de classes concretas.

### 7.2 Problemas de Renderização (Terminal)

- **Conflito `rich.Live` × `prompt_toolkit`**: dois renderers escrevendo ANSI sem coordenação. Durante streaming, `Live.update()` é chamado sem `run_in_terminal`, competindo com o prompt.
- **Resize (`SIGWINCH`)**: handlers dos dois sistemas intercalam sequências VT, corrompendo cursor e scroll.
- **Toolbar extrapolando**: conteúdo sem clipping ultrapassa a largura do terminal em sessões com muitos campos.
- **`refresh_interval=1.0`**: atualização da toolbar atrasada em até 1s após resize.

### 7.3 Violações de Fronteira de Camadas (Resolvidas)

Imports diretos de `profiles/` que existiam em módulos da camada de runtime/sandbox foram eliminados:

| Módulo | Violação | Status |
|---|---|---|
| `sandbox/bwrap.py` | importava `ExecutionProfile` de `profiles.base` | **Resolvido** (8c) |
| `runtime/task_planning.py` | importava `ExecutionProfile` de `profiles.base` | **Resolvido** (8a) |
| `runtime/drivers/repl.py` | chamava `_profile_registry.all_profiles()` diretamente | **Resolvido** (8b) |
| `app/core.py` | `delegate_for_parallel` exposto via wrapper desnecessário | **Resolvido** (8e) |
| `ui/renderer.py` | consultava `quimera.profiles.get(...)` para metadados | **Resolvido** (8d) |

### 7.4 Lookups de Profile Distribuídos (Resolvidos)

Os lookups dispersos de `quimera.profiles.get(...)` em `app/dispatch.py`, `app/task.py`, `app/system_layer.py` e `app/chat_round.py` foram resolvidos. Lookups remanescentes em `cli.py`, `agents/client.py` e `agents/text_filters.py` são legítimos — essas camadas têm acesso intencional ao registro de profiles.

### 7.5 Outros Problemas

- **Adaptadores legados em `system_layer.py`**: `_LegacyProfileResolver` e `_LegacyAgentPoolAdapter` indicam migração de contrato incompleta.
- **Cobertura de testes**: 2740+ testes passando. Lacunas em testes de integração de tasks (criação → execução → revisão), concorrência e cenários de falha.
- **Logging**: feito via `print` estruturado; sem níveis de gravidade padronizados ou saída JSON.
- **`renderer.py`** (~1200 linhas): beneficiaria divisão em módulos menores.

---

## 8. Resumo

### Pontos Fortes

- Separação conceitual entre camadas: apresentação, aplicação, domínio, infraestrutura.
- Input via `TextualInputGate` (TUI) ou `SimpleInputGate` (pipe); `InputGate.is_active()` como árbitro único de estado de prompt ativo.
- `_BACKWARD_MAP` removido: estado de runtime acessado diretamente via `AppRuntimeState`.
- Decomposição de `core.py` em andamento: 8 módulos extraídos, ~689 linhas reduzidas.
- Violações de fronteira entre camadas (seções 7.3 e 7.4) resolvidas.
- Sistema de tasks maduro (extraído para `tasks/`): revisão, failover, roteamento por especialidade.
- Arquitetura orientada a goals com critérios de aceitação e regras de revisão.
- Sistema de evidências para rastreamento de contexto verificável.
- Boa cobertura de testes unitários (2740+ testes).

### Problemas Prioritários

1. **Threading/renderização**: Textual gerencia o ciclo de renderização, mas eventos emitidos de threads de background competem com o loop do Textual. O `TextualUiBridge` usa `call_from_thread` para eventos imediatos e fila para eventos pré-montagem.
2. **`core.py` ainda grande**: ~1611 linhas com ~40 lambdas `lambda: self.*` no `__init__` — acoplamento alto que dificulta extração adicional.
3. **Adaptadores legados em `system_layer.py`**: `_LegacyProfileResolver` e `_LegacyAgentPoolAdapter` indicam migração de contrato incompleta.
