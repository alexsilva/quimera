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
│   ├── tty_control.py                # Controle de TTY (suspend/resume do renderer)
│   ├── toolbar.py                    # ToolbarManager: toolbar dinâmica do prompt_toolkit
│   ├── bug_services.py               # BugServices: detecção e correlação de bugs de runtime
│   ├── command_router.py             # Roteamento de comandos slash (/task, /help, etc.)
│   ├── chat_processor.py             # ChatProcessor: processamento de uma rodada de chat
│   ├── ui_event_handler.py           # UIEventHandler: processamento de eventos de UI
│   ├── chat_round.py                 # Lógica de uma rodada de chat (humano → agente → resultado)
│   ├── dispatch.py                   # Despacho de chamadas para agentes e tools
│   ├── task.py                       # Ponto de entrada de tarefas (coordenação de alto nível)
│   ├── task_execution_service.py     # Execução de tasks com pool de workers
│   ├── task_review_service.py        # Revisão de resultados de tasks por outro agente
│   ├── task_failover_policy.py       # Políticas de failover e retry de tasks
│   ├── task_repository.py            # Acesso ao banco de dados de tasks (SQLite)
│   ├── task_prompt_factory.py        # Criação de prompts específicos para tasks
│   ├── task_classifiers.py           # Classificação de resultados (ACCEPT/RETRY/REPLAN/REJECT)
│   ├── task_router.py                # Roteamento de tasks para o agente adequado
│   ├── task_utils.py                 # Utilitários de suporte a tasks
│   ├── task_events.py                # Definição de eventos do ciclo de vida de tasks
│   ├── agent_call_service.py         # Serviço de chamada a agentes (retry, timeout)
│   ├── agent_gateway.py              # Interface de baixo nível para AgentClient
│   ├── agent_pool.py                 # Gerenciamento de pool de agentes disponíveis
│   ├── worker.py                     # Worker thread de chat (isola exceções, recupera turno)
│   ├── tool_loop.py                  # Loop de execução de tools com timeout
│   ├── protocol.py                   # Parsing/geração do protocolo de handoff entre agentes
│   ├── event_sink.py                 # Sistema de publish-subscribe de eventos internos
│   ├── render_event.py               # Definição de eventos de renderização
│   ├── handlers.py                   # Handlers de eventos de UI
│   ├── display_service.py            # Serviço auxiliar de renderização (formatos, estilos)
│   ├── system_layer.py               # Processamento de comandos de sistema (/task, /help, ...)
│   ├── session.py                    # Gerenciamento de histórico e recuperação de sessão
│   ├── session_metrics.py            # Métricas de sessão (turnos, latências, etc.)
│   ├── turn.py                       # Gerenciamento de turno (humano ↔ agente)
│   ├── inputs.py                     # Integração com InputGate (prompt_toolkit)
│   ├── prompt_input.py               # InputGate: wrapper sobre prompt_toolkit.PromptSession
│   ├── interfaces.py                 # Protocolos/interfaces entre camadas
│   └── config.py                     # Configuração de nível de aplicação
│
├── runtime/                          # Executor de tools e runtime de agentes
│   ├── tasks.py                      # Definição da interface de tasks de runtime
│   ├── task_runner.py                # Execução de uma task individual
│   ├── task_executor.py              # Pool de execução de tasks do runtime
│   ├── task_reviewer.py              # Revisão de resultados no runtime
│   ├── task_planning.py              # Planejamento e decomposição de tasks
│   ├── executor.py                   # Executor genérico de operações do runtime
│   ├── parser.py                     # Parser de blocos de tool retornados por agentes
│   ├── streaming.py                  # Suporte a streaming de respostas de agentes
│   ├── registry.py                   # Registro de tools disponíveis no runtime
│   ├── tool_hops.py                  # Encadeamento de chamadas de tools (multi-hop)
│   ├── approval.py                   # Aprovação interativa de ações do runtime
│   ├── approve_summary.py            # Resumo de aprovações pendentes
│   ├── policy.py                     # Políticas de execução (permissões, sandboxing)
│   ├── models.py                     # Modelos de dados do runtime (Task, Result, etc.)
│   ├── errors.py                     # Exceções específicas do runtime
│   ├── config.py                     # Configuração do runtime (timeouts, limites)
│   ├── drivers/                      # Drivers de execução de agentes
│   │   ├── repl.py                   # Driver REPL (processo interativo persistente)
│   │   ├── openai_compat.py          # Driver compatível com API OpenAI
│   │   └── tool_schemas.py           # Schemas JSON de tools para drivers de API
│   └── tools/                        # Implementações de tools individuais
│       ├── shell.py                  # Tool de execução shell
│       ├── files.py                  # Tool de acesso ao filesystem
│       ├── web.py                    # Tool de acesso à web
│       ├── patch.py                  # Tool de aplicação de patches
│       └── tasks.py                  # Tool de criação/consulta de tasks
│
├── agents/                           # Infraestrutura de comunicação com agentes LLM
│   ├── client.py                     # Cliente unificado para agentes (CLI, API, plugin)
│   ├── parsers.py                    # Parsers de saída de agentes (JSON, markdown, etc.)
│   ├── process_runner.py             # Execução e gerenciamento de subprocessos de agentes
│   ├── signal_guard.py               # Proteção contra sinais durante chamadas de agentes
│   ├── text_filters.py               # Filtros de texto (strip de ruído, normalização)
│   └── warm_pool.py                  # Pool de processos pré-aquecidos de agentes
│
├── plugins/                          # Sistema de plugins por agente
│   ├── base.py                       # Registro central (AgentPlugin, _plugin_registry)
│   ├── claude.py                     # Plugin para Claude (Anthropic CLI)
│   ├── codex.py                      # Plugin para Codex (OpenAI CLI)
│   ├── gemini.py                     # Plugin para Gemini (Google CLI)
│   ├── ollama.py                     # Plugin para Ollama (LLMs locais)
│   ├── opencode.py                   # Plugin para OpenCode (vários backends)
│   ├── mock.py                       # Plugin mock para testes
│   └── spy_utils.py                  # Utilitários de espionagem/observação de plugins
│
├── ui/                               # Camada de apresentação
│   ├── renderer.py                   # TerminalRenderer (Rich + Rich.Live, ~1200 linhas)
│   └── audit.py                      # Logger de auditoria de eventos de renderização
│
├── domain/                           # Modelos de domínio (independentes de framework)
│   └── session_state.py              # SessionState thread-safe (RLock), estado da sessão
│
├── evidence/                         # Sistema de evidências (rastreamento de contexto)
│   ├── models.py                     # Modelos de evidência (EvidenceItem, etc.)
│   ├── store.py                      # Armazenamento e recuperação de evidências
│   ├── parser.py                     # Parser de blocos de evidência no texto
│   └── formatter.py                  # Formatação de evidências para o prompt
│
├── sandbox/                          # Isolamento de execução
│   └── bwrap.py                      # Sandbox via bubblewrap (bwrap), paths RW/RO
│
├── prompt.py                         # Injeção do bloco de execução no prompt (goal-driven)
├── prompt_budget.py                  # Orçamento de tokens: trunca seções por prioridade
├── prompt_templates.py               # Templates de prompt (seções, marcadores)
├── prompt_kinds.py                   # Tipos de prompt (chat, task, review, etc.)
├── context.py                        # Montagem dinâmica do contexto para prompts
├── shared_state.py                   # Dicionário de estado compartilhado entre agentes
├── storage.py                        # Persistência de sessão (logs, histórico em arquivo)
├── workspace.py                      # Representação do workspace do usuário
├── session_summary.py                # Sumarização de sessão para manter contexto compacto
├── bugs.py                           # Detecção, correlação e reporte de bugs de runtime
├── agent_events.py                   # Definição de eventos de agentes (AgentEvent, etc.)
├── handoff_presenter.py              # Formatação de handoffs para exibição
├── shared_state_presenter.py         # Formatação do shared state para exibição
├── execution_mode_presenter.py       # Formatação do modo de execução ativo
├── spy_output_presenter.py           # Formatação de saída de spy (debug de agentes)
├── memory_selector.py                # Seleção de memória relevante para o prompt
├── themes.py                         # Definição e carregamento de temas visuais
├── config.py                         # Configuração global do projeto
├── constants.py                      # Constantes (prompts de sistema, comandos, prefixos)
├── modes.py                          # Modos de operação (debug, produção, etc.)
├── metrics.py                        # Rastreamento de métricas de comportamento
├── env_config.py                     # Carregamento de variáveis de ambiente
├── paths.py                          # Resolução de paths (workspace, dados, config)
└── cli.py                            # Ponto de entrada CLI (argparse, bootstrap)
```

---

## 3. Camadas e Responsabilidades

### 3.1 Camada de Aplicação (`app/`)

- **`core.py`**: Orquestrador central. Loop principal, estado da aplicação, coordenação de componentes, despacho para agentes e tasks. Reduzido de ~2300 para ~1611 linhas após extração de módulos especializados (ver abaixo).
- **`runtime_state.py`** (`AppRuntimeState`): Container de estado de runtime: status de input não-bloqueante, contadores de chat, semáforo de slots, executor de threads. Substitui o `_BACKWARD_MAP` legado que redirecionava atributos privados.
- **`session_bootstrap.py`**: Inicialização da sessão: resolução de paths (logs, bugs, debug), análise de bugs de sessão anterior. Extraído de `core.py` para isolar lógica de startup.
- **`tty_control.py`**: Controle de TTY: suspend e resume do renderer durante operações bloqueantes (editor, aprovação interativa). Coordena `TerminalRenderer` + `InputGate`.
- **`toolbar.py`** (`ToolbarManager`): Geração e atualização da toolbar dinâmica do `prompt_toolkit`. Renderiza estado atual (agentes, turno, tema, métricas).
- **`bug_services.py`** (`BugServices`): Detecção, correlação e reporte de bugs de runtime (burst de falhas, timeouts). Extraído de `AppBugServices` via rename + flatten.
- **`command_router.py`**: Roteamento de comandos slash internos. Mantém o mapeamento de `/cmd` → handler sem exposição direta ao `core.py`.
- **`chat_processor.py`** (`ChatProcessor`): Processamento de uma rodada de chat completa: input → agente → resultado → update de estado. Orquestra workers e sincronização.
- **`ui_event_handler.py`** (`UIEventHandler`): Processamento de eventos da fila de UI (`ui_event_queue`). Usa `InputGate.is_active()` como fonte primária para decidir se o prompt deve ser redesenhado.
- **`chat_round.py`**: Encapsula a lógica de uma rodada de chat completa (leitura de input → chamada ao agente → processamento de resultado → update de estado).
- **`dispatch.py`**: Chama agentes via `AgentClient` e executa tools via `ToolLoop`. Gerencia o ciclo de vida de chamadas e resultados.
- **`task*.py`**: Conjunto de serviços que implementam o ciclo de vida de tasks: criação, classificação, atribuição, execução, revisão, failover e notificação.
- **`prompt_input.py`** (`InputGate`): Wrapper sobre `prompt_toolkit.PromptSession`. Gerencia o prompt interativo do usuário, histórico e coordenação com o renderer. `InputGate.is_active()` é a fonte primária de verdade para estado de prompt ativo (substituiu `nonblocking_input_status` como árbitro principal). Fallback para `input()` built-in apenas quando `_session` é `None` (contextos de teste).
- **`inputs.py`**: Integração de alto nível com `InputGate`, exposta ao `core.py`.
- **`event_sink.py`**: Publish-subscribe interno. Eventos publicados de worker threads são enfileirados na `ui_event_queue`; publicados da main thread são processados diretamente.
- **`system_layer.py`**: Processa comandos `/cmd` do usuário. Adaptadores legados (`_LegacyPluginResolver`, `_LegacyAgentPoolAdapter`) mantidos para compatibilidade de migração.
- **`protocol.py`**: Define e parseia o formato de handoff entre agentes (`type`, `route`, `content`, `metadata`).
- **`interfaces.py`**: Protocolos (typing) que tentam estabelecer contratos entre camadas — ainda subutilizados.

### 3.2 Camada de Runtime (`runtime/`)

Executor de tools e agentes em ambiente potencialmente sandboxed.

- **`drivers/`**: Drivers de execução de agentes. `repl.py` gerencia processos interativos persistentes; `openai_compat.py` suporta a API OpenAI; `tool_schemas.py` define schemas JSON para uso em API mode.
- **`tools/`**: Implementações individuais de tools: shell, filesystem, web, patch, tasks.
- **`task_planning.py`**: Decompõe goals em tasks individuais.
- **`approval.py`**: Solicita aprovação interativa do usuário antes de ações potencialmente destrutivas.
- **`policy.py`**: Define o que pode ou não ser executado (permissões de sandbox).

### 3.3 Camada de Agentes (`agents/`)

- **`client.py`**: Interface unificada para todos os backends (CLI local, API remota, plugin). Suporta streaming.
- **`warm_pool.py`**: Pool de processos pré-iniciados para reduzir latência de cold start.
- **`process_runner.py`**: Gerencia subprocessos de agentes (stdin/stdout/stderr, timeout, sinalização).

### 3.4 Camada de Plugins (`plugins/`)

- **`base.py`**: Define `AgentPlugin` (dataclass com metadados: `name`, `driver`, `supports_task_execution`, `runtime_rw_paths`, etc.) e `_plugin_registry`.
- Plugins concretos (`claude.py`, `codex.py`, `gemini.py`, `ollama.py`, `opencode.py`): cada um registra um `AgentPlugin` com configuração específica do backend.
- `mock.py`: Plugin para testes unitários.

### 3.5 Camada de Apresentação (`ui/`)

- **`renderer.py`** (`TerminalRenderer`): Renderizador terminal usando `Rich` e `Rich.Live`. Gerencia buffer de streaming, temas, scrollback e atualizações em tempo real. Deve ser acessado **apenas** da main thread.
- **`audit.py`**: Registra eventos de renderização para depuração.

### 3.6 Domínio (`domain/`)

- **`session_state.py`** (`SessionState`): Container thread-safe (`threading.RLock`) para o estado corrente da sessão. Compartilhado entre main thread e workers.

### 3.7 Evidências (`evidence/`)

Sistema de rastreamento de contexto verificável. Evidências são extraídas das respostas dos agentes, armazenadas em `store.py` e injetadas no prompt via `formatter.py` para fornecer contexto verificável nas próximas rodadas.

### 3.8 Sandbox (`sandbox/`)

- **`bwrap.py`**: Integração com `bubblewrap` (bwrap) para isolamento de processos. Define paths de leitura/escrita permitidos por agente via `AgentPlugin.runtime_rw_paths`.

### 3.9 Sistema MCP (Model Context Protocol)

O Quimera implementa o protocolo MCP (`2025-11-25`, com negociação para versões anteriores) para expor as ferramentas do runtime a agentes compatíveis. O servidor MCP é iniciado por sessão em um socket Unix com autenticação por token.

#### 3.9.1 Componentes

| Módulo | Arquivo | Responsabilidade |
|---|---|---|
| **MCPServer** | `runtime/mcp/server.py` | Servidor JSON-RPC 2.0 sobre stdio/socket/HTTP. Métodos principais: lifecycle, `ping`, tools, resources, prompts, completion e logging |
| **ToolExecutor** | `runtime/executor.py` | Executa tools com validação de política, aprovação e resolução de aliases |
| **ToolRegistry** | `runtime/registry.py` | Registro nome → handler (dict simples) |
| **HandoffTools** | `runtime/tools/handoff.py` | Implementa `call_agent` — delegação cross-MCP entre agentes |
| **Proxy stdio→socket** | `runtime/mcp/server.py:_proxy_stdio_to_socket` | Ponte transparente entre stdio do agente e socket Unix do servidor |
| **Plugin MCP injection** | `plugins/{claude,codex,opencode}.py` | Cada plugin injeta config MCP no formato nativo do agente |
| **Tool schemas** | `runtime/drivers/tool_schemas.py` | Fonte única de schemas: `resolve_tool_schemas()` filtra por registro/política |
| **Prompt conditionals** | `prompt.md`, `task_prompt.md` | Blocos `<!-- IF:mcp_enabled -->` ativam instruções MCP nos prompts |
| **Config bridge** | `app/core.py:configure_mcp_socket()` / `configure_mcp_http()` | Propaga socket/http endpoint e token para todos os plugins ativos |

#### 3.9.2 Fluxo de Inicialização

```
CLI (socket padrão)
  ├── Usa token de `--mcp-token-env`/`QUIMERA_MCP_TOKEN` quando definido; caso contrário gera token com secrets.token_urlsafe(32)
  ├── Socket padrão: workspace.tmp.root / f"mcp-{rand}.sock"; `--mcp-socket [path]` permite selecionar/definir path
  ├── HTTP opcional: `--mcp-http --mcp-host 127.0.0.1 --mcp-port 9090` expõe `/mcp`; `--mcp-token-env` permite token fixo para clientes remotos
  ├── Cria MCPServer(tool_executor, auth_token=mcp_token)
  ├── inicia socket ou MCP_HTTPServer em background
  ├── app.configure_mcp_socket(...) ou app.configure_mcp_http(...) → propaga para plugins
  └── session_state["mcp_enabled"] = True      → ativa blocos no prompt
```

#### 3.9.3 Injeção por Plugin

| Plugin | Formato | Exemplo |
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

#### 3.9.5 Cross-MCP (call_agent)

A ferramenta `call_agent` é o mecanismo central de interoperabilidade entre agentes no pool:

- **Disponibilidade**: verifica se `_call_agent_fn` foi injetado pelo `ToolExecutor.set_call_agent_fn()`.
- **Parâmetros**: `agent_name` (obrigatório), `task` (obrigatório), `context`, `fallback_agents`, `handoffs`.
- **Validação**: agente alvo deve estar no pool ativo. `handoffs` suporta cadeias multi-passo.
- **Execução**: dispatch via `dispatch_services.call_agent()` com `protocol_mode="handoff"`, `silent=False`.
- **Truncamento**: task limitada a 1200 caracteres, contexto a 4000.

#### 3.9.6 MCP-First Mode

Em `app/protocol.py:232`, quando MCP está ativo, envelopes textuais legados (`<handoff>...</handoff>`) são ignorados — agentes devem usar exclusivamente `call_agent`. Isso evita dupla delegação.

#### 3.9.7 Ferramentas Expostas via tools/list

Tools definidas em `TOOL_SCHEMAS`, filtradas por:
1. Registro no executor (intersecção com handlers registrados)
2. Configuração (tools de task ocultas sem db_path)
3. Política (tools bloqueadas removidas)
4. Disponibilidade de `call_agent` (oculta se fn não injetada)

### 3.10 Módulos Raiz de Prompt

| Módulo | Responsabilidade |
|---|---|
| `prompt.py` | Injeta bloco de execução goal-driven no topo do prompt quando `goal_canonical` está ativo |
| `prompt_budget.py` | Gerencia orçamento de tokens: trunca seções por prioridade quando necessário |
| `prompt_templates.py` | Templates base: seções obrigatórias/opcionais, marcadores XML |
| `prompt_kinds.py` | Tipos de prompt por contexto (chat, task, review, handoff) |
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
| `*_presenter.py` | Formatadores de seções específicas do prompt (handoff, shared state, etc.) |

---

## 4. Modelo de Threading e Concorrência

### 4.1 Main Thread (UI Thread)

- Executa o loop principal (`core.py:run()`).
- Lê input do usuário via `InputGate` (wrapper sobre `prompt_toolkit`).
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
  - `TaskExecutionService` (`app/task_execution_service.py`): pool para tasks.
  - `ToolLoop` (`app/tool_loop.py`): execução individual com timeout.
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
| `InputGate.is_active()` | `app/prompt_input.py` | Árbitro primário de estado de prompt ativo (substitui `nonblocking_input_status` como fonte de verdade) |
| `AppRuntimeState` | `app/runtime_state.py` | Estado de runtime (slots, contadores, semáforo) — sem mais atributos privados via `_BACKWARD_MAP` |

### 4.4 Problemas Conhecidos de Threading

- `TerminalRenderer` não é thread-safe. Apesar da `ui_event_queue`, alguns caminhos em `display_service.py` e `agent_gateway.py` ainda chamam métodos do renderer diretamente de worker threads.
- Conflito entre `rich.Live` e `prompt_toolkit`: dois renderers escrevendo sequências ANSI no mesmo terminal sem coordenação. Em eventos de resize (`SIGWINCH`), os handlers dos dois sistemas intercalam, corrompendo cursor e scroll.
- A toolbar do `prompt_toolkit` (`refresh_interval=1.0`) só atualiza a cada segundo, causando atraso perceptível após resize ou mudança de tema.
- **Extrapolação da toolbar**: o conteúdo da toolbar pode ultrapassar a largura do terminal quando há muitos campos (session_id, theme, model, turns, etc.) — o `prompt_toolkit` não faz clipping automático.

---

## 5. Fluxo de Controle Principal

O loop em `quimera/app/core.py:run()` segue este fluxo:

1. **Inicialização**: carrega configuração, plugins, estado de sessão; cria renderer, InputGate, TurnManager, pool de workers, `ui_event_queue`. O servidor MCP é iniciado em `cli.py` antes de `app.run()` — resolve token de autenticação por env ou gera token aleatório, cria socket Unix, inicia MCPServer em background e propaga configuração para todos os plugins ativos.
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
- `plugins/` define metadados por agente: capacidades, paths de sandbox, driver, tipos de task suportados, e mecanismo de injeção MCP via `mcp_server_args()`.
- `agents/warm_pool.py` mantém processos pré-aquecidos para reduzir latência.
- `runtime/mcp/server.py` expõe as ferramentas do runtime via protocolo MCP, permitindo que agentes executem tools sem depender do parser textual de tool calls.
- `plugins/{claude,codex,opencode}.py` cada um implementa `mcp_server_args(socket_path)` para injetar a configuração MCP no formato nativo do agente (JSON, CLI args, env vars).

### 6.2 Sistema de Tasks

Fluxo: `/task` → `TaskRepository` (criação) → `task_classifiers` (tipo) → `task_router` (agente) → `task_execution_service` (execução em worker) → `task_review_service` (revisão opcional) → `task_failover_policy` (retry/reject) → notificação via `ui_event_queue`.

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

### 6.4 Protocolo de Handoff entre Agentes

O Quimera suporta dois mecanismos de handoff:

#### 6.4.1 Handoff via MCP (call_agent) — Preferencial

Quando o MCP está ativo (padrão), os agentes usam a tool `call_agent` exposta via protocolo MCP. Definido em `runtime/tools/handoff.py`:

```json
{
  "name": "call_agent",
  "arguments": {
    "agent_name": "codex",
    "task": "Implementar função de parser",
    "context": "contexto opcional",
    "fallback_agents": ["claude"],
    "handoffs": [{"agent_name": "claude", "task": "Revise o resultado"}]
  }
}
```

- **Disponibilidade**: depende de `_call_agent_fn` injetado pelo app (`ToolExecutor.set_call_agent_fn()`).
- **Validação**: o alvo é verificado contra `_resolve_active_agents()` no pool.
- **Failover**: `fallback_agents` tentado em sequência se o primário falhar.
- **Cadeias**: `handoffs` executa passos adicionais, cada um com seu próprio fallback.

#### 6.4.2 Handoff Textual Legado

Formato definido em `app/protocol.py`:

```json
{
  "type": "handoff",
  "route": "nome-do-agente",
  "content": "descrição da tarefa",
  "metadata": {"context": "...", "expected": "..."}
}
```

Handoffs em sequência usam `"handoffs": [...]`. O sistema atualiza `shared_state` e histórico automaticamente.

**Nota**: Quando MCP está ativo, o protocolo opera em **MCP-first mode** — envelopes textuais legados (`<handoff>...</handoff>`) são ignorados pelo parser, evitando dupla delegação.

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

Imports diretos de `plugins/` que existiam em módulos da camada de runtime/sandbox foram eliminados:

| Módulo | Violação | Status |
|---|---|---|
| `sandbox/bwrap.py` | importava `AgentPlugin` de `plugins.base` | **Resolvido** (8c) |
| `runtime/task_planning.py` | importava `AgentPlugin` de `plugins.base` | **Resolvido** (8a) |
| `runtime/drivers/repl.py` | chamava `_plugin_registry.all_plugins()` diretamente | **Resolvido** (8b) |
| `app/core.py` | `call_agent_for_parallel` exposto via wrapper desnecessário | **Resolvido** (8e) |
| `ui/renderer.py` | consultava `quimera.plugins.get(...)` para metadados | **Resolvido** (8d) |

### 7.4 Lookups de Plugin Distribuídos (Resolvidos)

Os lookups dispersos de `quimera.plugins.get(...)` em `app/dispatch.py`, `app/task.py`, `app/system_layer.py` e `app/chat_round.py` foram resolvidos. Lookups remanescentes em `cli.py`, `agents/client.py` e `agents/text_filters.py` são legítimos — essas camadas têm acesso intencional ao registro de plugins.

### 7.5 Outros Problemas

- **Adaptadores legados em `system_layer.py`**: `_LegacyPluginResolver` e `_LegacyAgentPoolAdapter` indicam migração de contrato incompleta.
- **Cobertura de testes**: 2209 passando. Lacunas em testes de integração de tasks (criação → execução → revisão), concorrência e cenários de falha.
- **Logging**: feito via `print` estruturado; sem níveis de gravidade padronizados ou saída JSON.
- **`renderer.py`** (~1200 linhas): beneficiaria divisão em módulos menores.

---

## 8. Resumo

### Pontos Fortes

- Separação conceitual entre camadas: apresentação, aplicação, domínio, infraestrutura.
- Input 100% via `prompt_toolkit`; `InputGate.is_active()` como árbitro único de estado de prompt ativo.
- `_BACKWARD_MAP` removido: estado de runtime acessado diretamente via `AppRuntimeState`.
- Decomposição de `core.py` em andamento: 8 módulos extraídos, ~689 linhas reduzidas.
- Violações de fronteira entre camadas (seções 7.3 e 7.4) resolvidas.
- Sistema de tasks maduro: revisão, failover, roteamento por especialidade.
- Arquitetura orientada a goals com critérios de aceitação e regras de revisão.
- Sistema de evidências para rastreamento de contexto verificável.
- Boa cobertura de testes unitários (2209 testes).

### Problemas Prioritários

1. **Threading/renderização**: conflito `rich.Live` × `prompt_toolkit` pode causar corrupção de terminal em resize durante streaming. A suspensão do renderer foi estabilizada para o fluxo do editor (`tty_control.py`), mas o caminho geral ainda não usa `run_in_terminal`.
2. **`core.py` ainda grande**: ~1611 linhas com ~40 lambdas `lambda: self.*` no `__init__` — acoplamento alto que dificulta extração adicional.
3. **Adaptadores legados em `system_layer.py`**: `_LegacyPluginResolver` e `_LegacyAgentPoolAdapter` indicam migração de contrato incompleta.
