# PLAN: Refatoração arquitetural de `quimera/app/core.py`

Autor: CLAUDE-FABLE · Data: 2026-07-12 · Branch alvo: `main-ui`

## Diagnóstico (resumo)

`QuimeraApp` (1206 LOC) acumula quatro papéis que precisam ser separados:

1. **Composition root** — `__init__` de ~520 linhas cria e conecta ~30 colaboradores
   com acoplamento por ordem (ex.: `task_services` nasce em `core.py:414` antes de
   `dispatch_services` em `core.py:472` e recebe `bind_dispatch_services` depois).
2. **Estado de runtime** — três contêineres sobrepostos: `self.session_state` (dict,
   `core.py:321`), `self._chat_state` (`SessionState`, `core.py:343`) e
   `self.session_state_mgr` (`SessionStateManager`, `core.py:220`), compartilhando os
   mesmos objetos mutáveis com locks distintos.
3. **DI informal por lambda** — ~25 lambdas capturando `self` (`lambda: self.tool_executor`,
   `lambda: self.execution_mode`, `lambda: self.dispatch_services`…) para contornar
   ciclos de construção. Escondem contratos e quebram silenciosamente em renomeações.
4. **Fachada legada** — ~20 métodos de repasse (`delegate`, `print_response`,
   `handle_command`, `parse_routing`…) cheios de `getattr(self, ...)` defensivo
   (33 ocorrências) para atributos que sempre existem após o `__init__`.

Agravantes: `__del__` para cleanup (`core.py:846`), mutação de `os.environ`
(`core.py:381`), lógica de UI de baixo nível dentro do core (`_make_ask_user_fn`,
`core.py:958`).

## Princípios do redesenho

- **Construção em fases explícitas, sem lambdas.** Todo ciclo real vira uma chamada
  `wire()` única e visível; toda dependência tardia legítima vira um *holder* tipado.
- **Uma única fonte de verdade para estado de sessão.**
- **O core vira fachada fina (< 200 LOC).** Ninguém novo depende dela; módulos novos
  dependem dos serviços diretamente.
- **Lifecycle explícito e idempotente** (`close()`), nunca `__del__`.
- **Contratos via `Protocol` estreitos**, não via `Any` + `getattr`.

## Componentes-alvo

```
quimera/app/
├── bootstrap/
│   ├── context.py      # AppOptions (frozen dataclass com params do CLI)
│   ├── wiring.py       # AppAssembler: build_* em ordem fixa, retorna bundles
│   └── bundles.py      # dataclasses imutáveis: PlatformBundle, UiBundle,
│                       #   SessionBundle, RuntimeBundle, TaskBundle, ChatBundle
├── state/
│   ├── session_state.py    # SessionRuntimeState — fonte única (ver §2)
│   └── execution_mode.py   # ExecutionModeState — holder observável (ver §3)
├── lifecycle.py        # AppLifecycle: start()/close() idempotentes, JobEnvGuard
├── core.py             # QuimeraApp fachada fina + run()
└── (demais serviços já existentes, com assinaturas enxugadas)
```

### 1. `AppAssembler` (bootstrap/wiring.py)

O `__init__` atual vira seis builders puros, cada um recebendo **apenas os bundles
anteriores** e devolvendo um dataclass imutável:

```python
class AppAssembler:
    def assemble(self, opts: AppOptions) -> AppBundles:
        platform = self._build_platform(opts)      # Workspace, ConfigManager,
                                                   # SessionStorage, EnvConfig, policy
        ui       = self._build_ui(opts, platform)  # Renderer, InputGate, InputBroker,
                                                   # DisplayService, Toolbar*
        session  = self._build_session(platform, ui)   # SessionRuntimeState,
                                                       # ContextManager, PromptBuilder
        runtime  = self._build_runtime(opts, platform, ui, session)
                                                   # AgentClient, ProcessSupervisor,
                                                   # ToolExecutor, DispatchServices
        tasks    = self._build_tasks(platform, ui, session, runtime)
        chat     = self._build_chat(ui, session, runtime, tasks)
                                                   # ChatRoundOrchestrator,
                                                   # ChatLifecycle, UiEventHandler
        self._wire(runtime, tasks, chat)           # ÚNICO ponto de resolução de ciclos
        return AppBundles(platform, ui, session, runtime, tasks, chat)
```

Regras:
- Ordem dos builders é a única fonte de ordem — nenhum builder lê atributo que
  ainda não existe.
- `_wire()` concentra as ligações hoje espalhadas em `bind_dispatch_services`,
  `bind_dispatch_tool_executor`, `set_delegate_fn`, `set_ask_user_fn` etc. — todas
  com objetos reais, não getters.
- Cada bundle é `@dataclass(frozen=True)`; testes podem montar bundles parciais
  com fakes sem instanciar o app inteiro.

### 2. `SessionRuntimeState` (state/session_state.py)

Funde os três contêineres atuais em um único objeto:

```python
class SessionRuntimeState:
    """Fonte única de verdade da sessão: history, shared_state, métricas, meta."""
    history: list[Message]
    shared_state: dict
    meta: SessionMeta            # dataclass: session_id, current_job_id, counters…
    metrics: SessionMetrics      # antes espalhado no dict session_state
    turn_stamps: dict
    # locks internos, únicos:
    _history_lock: RLock
    _shared_state_lock: RLock

    def history_snapshot(self) -> list: ...
    def shared_state_snapshot(self) -> dict: ...
    def record_delegation(self, ok: bool) -> None: ...   # substitui mutação direta do dict
```

- `SessionStateManager` vira **persistência apenas** (`SessionPersistence`):
  recebe `SessionRuntimeState` + `SessionStorage`, expõe `save()`/`load()`.
  Deixa de ser dono de locks.
- `SessionState` (domain) é absorvido ou vira uma *view* somente-leitura para o
  chat round — nunca um segundo dono dos mesmos objetos.
- O dict `self.session_state` desaparece; quem lia chaves soltas passa a ler
  `state.meta.*` / `state.metrics.*` (migração mecânica, ver Fase 1).

### 3. `ExecutionModeState` e fim das lambdas

Só existem **três** dependências genuinamente tardias hoje; cada uma ganha um
mecanismo explícito:

| Padrão atual | Substituto |
|---|---|
| `lambda: self.execution_mode` (4 usos) | `ExecutionModeState` com `.get()`, `.set(mode)` e listeners registrados (agent_client, tool_executor.policy, toolbar). `_set_execution_mode` some. |
| `lambda: self.tool_executor`, `lambda: self.dispatch_services`, `lambda: self.session_services` | Eliminação do ciclo via `_wire()`: os consumidores ganham método `wire(dispatch=… , tool_executor=…)` chamado uma única vez com objetos reais. Se o consumidor for usado antes do wire, é bug de programação → `raise NotWiredError`, não fallback silencioso. |
| `lambda v: setattr(self.runtime_state, 'nonblocking_…', v)` (4 usos em `AppInputServices`) | Passar o próprio `AppRuntimeState` (ou um `PromptState` extraído dele) — o serviço muta o objeto diretamente. |
| `lambda: list(self.agent_pool.agents)`, `lambda: self.agent_pool.orchestrator_agent` | Passar o próprio `AgentPool`; consumidores declaram `Protocol` `ActiveAgentsProvider` com `agents` e `orchestrator_agent`. |
| callbacks `show_error/show_warning/show_muted/notify_retry` repetidos em 5 construtores | Um único `Protocol MessageSink` (implementado por `AppSystemLayer`) passado como um parâmetro. |

Meta verificável: `rg -c "lambda" quimera/app/bootstrap/ quimera/app/core.py` ≤ 2.

### 4. Enxugar assinaturas gigantes

- `AppTaskServices` (50+ kwargs) é o maior sintoma. Dividir em:
  - `TaskCommandService` — parsing/handling de `/task` (UI-facing);
  - `TaskExecutionService` — execução e retries;
  - `TaskExecutorPool` — ciclo de vida dos executores (start/stop/claim_gate).
  Cada um com ≤ 8 dependências, agrupadas nos bundles.
- `AppDispatchServices`: os 8 callbacks `record_*`/`notify_*` viram `MessageSink`
  + `MetricsRecorder` (2 protocolos).
- Constantes de retry (`MAX_RETRIES`, `RETRY_BACKOFF_SECONDS`) saem de atributos
  de classe do app para um `RetryPolicy` dataclass no `RuntimeBundle`.

### 5. `AppLifecycle` (lifecycle.py)

```python
class AppLifecycle:
    def start(self) -> None: ...   # init_db, add_job, JobEnvGuard.enter, task executors
    def close(self) -> None: ...   # idempotente: stop executors, restore env,
                                   # save history, shutdown supervisor
    def __enter__/__exit__: ...
```

- `__del__` é removido; `run_chat_loop` passa a rodar dentro de
  `with app.lifecycle:`.
- `JobEnvGuard` encapsula `QUIMERA_CURRENT_JOB_ID` (set no enter, restore no exit)
  — hoje em `core.py:380` e `core.py:1184`.

### 6. Fachada `QuimeraApp` residual

- Mantém: `run()`, propriedades públicas usadas por testes/CLI (`renderer`,
  `agent_pool`, `workspace`, `config`…) delegando aos bundles.
- Métodos de repasse legados ficam, mas viram delegação direta **sem `getattr`**
  (pós-assemble tudo existe) e com `DeprecationWarning` comentado no docstring
  apontando o serviço real.
- `_make_ask_user_fn` migra para `inputs.py` como classe `AskUserPrompter`
  (recebe `InputGate` + `Renderer`); `_redisplay_user_prompt_if_needed` e
  `clear_terminal_screen` migram para `DisplayService`.
- `configure_mcp_socket/http` migram para `ProfileResolverAdapter` (já têm o
  caminho novo lá; os fallbacks com `getattr` em `core.py:726-750` morrem).

## Migração em fases (cada fase = 1 commit, suíte verde)

**Fase 0 — rede de segurança** *(pequena)*
Teste de caracterização: instanciar `QuimeraApp` com fakes (renderer_override +
input_gate_factory fake, já suportados) e snapshotar o grafo de atributos públicos.
Garante que a refatoração não muda a API observável.

**Fase 1 — unificar estado de sessão** *(maior valor/risco; fazer primeiro)*
Criar `SessionRuntimeState`; `_chat_state` e `session_state_mgr` passam a envolvê-lo;
o dict `session_state` vira propriedade de compatibilidade que lê de `meta`/`metrics`.
Locks reduzidos a um par único. ~6 arquivos tocados.

**Fase 2 — extrair builders** *(mecânica, grande em linhas, baixa em risco)*
Mover o corpo do `__init__` para `AppAssembler` sem mudar comportamento; `__init__`
vira `bundles = AppAssembler().assemble(opts)` + atribuições de compatibilidade.

**Fase 3 — matar lambdas e binds tardios**
Introduzir `ExecutionModeState`, `MessageSink`, `wire()`/`NotWiredError`; remover
`bind_*` públicos e as ~25 lambdas. Ajustar `AppTaskServices`/`AppDispatchServices`
para os novos protocolos (ainda sem dividi-los).

**Fase 4 — dividir `AppTaskServices`** *(pode ser paralelizada por outro agente)*
Split em `TaskCommandService`/`TaskExecutionService`/`TaskExecutorPool`.

**Fase 5 — lifecycle + limpeza da fachada**
`AppLifecycle`, remoção de `__del__`, `JobEnvGuard`, remoção dos 33 `getattr`
defensivos, migração de `_make_ask_user_fn` e afins para os módulos de UI.

## Riscos e mitigação

- **Ordem de inicialização escondida**: builders tornam a ordem explícita, e a
  Fase 0 detecta regressões de atributo faltante.
- **Testes que tocam atributos privados** (`_chat_state`, `session_state_mgr`):
  manter aliases de compatibilidade por uma fase e removê-los na Fase 5.
- **Threads vivas durante close()**: `AppLifecycle.close()` deve seguir a ordem
  inversa do start (executors → supervisor → persistence) e ser testado com o
  fake de executor existente.

## Critérios de aceite

- `core.py` < 250 LOC; nenhum `lambda` em wiring; nenhum `getattr(self, …)`
  defensivo; um único contêiner de estado de sessão; `close()` idempotente
  substituindo `__del__`; suíte completa verde (hoje: 2795 passed, 4 skipped).
