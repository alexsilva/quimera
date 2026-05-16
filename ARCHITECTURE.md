# Quimera — Arquitetura de Referência

> Documento de design. Descreve o estado atual, os problemas estruturais, e a arquitetura alvo.
> Toda mudança de arquitetura deve ser validada contra este documento antes de ser implementada.

---

## 1. Diagnóstico do estado atual

### 1.1 Monolito implícito

`QuimeraApp` (`app/core.py`) contém e instancia diretamente:

```
QuimeraApp
  ├── renderer          (TerminalRenderer)
  ├── event_sink        (EventSink)
  ├── system_layer      (AppSystemLayer)        ← recebe self (app)
  ├── protocol          (AppProtocol)           ← recebe self (app)
  ├── session_metrics   (SessionMetricsService)
  ├── task_services     (AppTaskServices)       ← recebe self (app)
  ├── dispatch_services (AppDispatchServices)   ← recebe self (app)
  ├── session_services  (AppSessionServices)    ← recebe self (app)
  ├── input_gate        (InputGate)
  ├── input_services    (AppInputServices)      ← recebe self (app)
  ├── chat_round_orchestrator (ChatRoundOrchestrator) ← recebe self (app)
  ├── context_manager   (ContextManager)
  ├── agent_client      (AgentClient)
  ├── storage           (SessionStorage)
  ├── config            (ConfigManager)
  ├── prompt_builder    (PromptBuilder)
  └── turn_manager      (TurnManager)
```

Cada serviço que recebe `app` acessa `app.renderer`, `app.storage`, `app.history`, etc.
diretamente — sem contrato formal. Isso cria acoplamento total.

### 1.2 Problemas concretos

| Problema | Manifestação |
|---|---|
| Acoplamento total | Qualquer serviço pode alterar qualquer estado de `app` |
| Threading frágil | Worker thread chama `renderer` diretamente → disputa pelo terminal |
| `_process_chat_queue` não sobrevive a exceções | Worker morre silenciosamente, trava o spin loop |
| `turn_manager.next_turn()` antes de `chat_queue.put()` | Janela de corrida entre turno e entrega da mensagem |
| `renderer.flush()` com timeout de 5s chamado de múltiplas threads | `TimeoutError` pode matar o worker |
| Locks expostos como atributos públicos de `app` | Qualquer serviço usa `app._lock`, `app._output_lock`, etc. |
| Construção lazy com `getattr` defensivo | `_get_gateway()`, `_get_tool_loop()` constroem dependências na hora errada |

---

## 2. Princípios da arquitetura alvo

1. **Cada classe recebe só o que precisa** — sem passar `app` inteiro.
2. **Renderer é exclusivo da main thread** — workers produzem eventos em fila; renderer consome.
3. **Estado compartilhado via contentor thread-safe** — não via atributos dispersos em `app`.
4. **Contratos explícitos (Protocol)** — interfaces definem o que cada camada expõe.
5. **Worker thread nunca morre silenciosamente** — exceções são capturadas, logadas e turno é restaurado.
6. **Migração incremental** — a arquitetura alvo não exige reescrita total; cada componente pode ser migrado isoladamente.

---

## 3. Mapa de camadas

```
┌─────────────────────────────────────────────────────┐
│  PRESENTATION (main thread only)                    │
│  TerminalRenderer · InputGate · EventSink           │
├─────────────────────────────────────────────────────┤
│  APPLICATION                                        │
│  ChatRoundOrchestrator · AppSystemLayer             │
│  AppDispatchServices · AppTaskServices              │
│  AppProtocol · ChatWorkerPool                       │
├─────────────────────────────────────────────────────┤
│  DOMAIN                                             │
│  TurnManager · SessionState · History               │
│  SharedState · TaskRepository                       │
├─────────────────────────────────────────────────────┤
│  INFRASTRUCTURE                                     │
│  AgentClient · SessionStorage · PluginRegistry      │
│  PromptBuilder · ContextManager · Workspace         │
└─────────────────────────────────────────────────────┘
```

**Regra de dependência:** camadas só podem importar das camadas abaixo.
Application → Domain → Infrastructure. Presentation → Application.
**Nenhuma camada importa de `core.py`.**

---

## 4. Modelo de threading

### 4.1 Problema central

O modelo atual (`threads > 1`) cria um worker thread que chama o renderer diretamente.
O renderer não é thread-safe para output concorrente — ele assume um "dono" do terminal
via `_output_lock` e `_prompt_owning_thread_id`.

### 4.2 Arquitetura alvo: Producer / Consumer

```
Main Thread                         Worker Thread(s)
──────────────                      ──────────────────
InputGate.read()                    ChatWorker._run()
  │                                   │
  └─► input_queue.put(msg)            └─► call_agent(...)
                                          │
Main loop picks up                        └─► ui_event_queue.put(RenderEvent)
  │
  ├─► process_input_queue()          ← dequeue de mensagens do usuário
  │
  └─► process_ui_event_queue()       ← dequeue de eventos de render
        │
        └─► renderer.show_*(...)     ← único ponto de escrita no terminal
```

**Invariantes:**
- `renderer.show_*()` é chamado **somente** no main thread.
- Worker threads se comunicam com o main thread **somente** via `ui_event_queue`.
- `TurnManager` permanece como sincronizador entre main e worker.

### 4.3 `ui_event_queue` — tipos de eventos

```python
@dataclass
class RenderEvent:
    """Evento de UI produzido por worker, consumido pelo main thread."""
    kind: str          # "system" | "agent_msg" | "warning" | "error" | "status"
    content: str
    agent: str | None = None
    metadata: dict | None = None
```

### 4.4 Escopo real de M3: produtores que chamam renderer diretamente

Criar `ui_event_queue` só em `core.py` **não fecha** o invariante "renderer exclusivo da main thread".
Os pontos abaixo também precisam ser adaptados para enfileirar `RenderEvent` em vez de chamar renderer:

| Arquivo | Linha | O que faz |
|---|---|---|
| `app/dispatch.py` | ~130 | `show_system_neutral` para progresso de tool hop |
| `app/dispatch.py` | ~349 | imprime resposta do agente direto no renderer |
| `app/agent_gateway.py` | ~148 | `flush()` e redisplay do prompt após chamada |
| `app/chat_round.py` | ~126 | chamada direta ao renderer em alguns fluxos |
| `app/chat_round.py` | ~223 | chamada direta ao renderer em alguns fluxos |

O critério de saída de M3 é: `grep -rn "renderer\." app/dispatch.py app/agent_gateway.py app/chat_round.py` retorna zero chamadas a `show_*`.

### 4.5 Watchdog para fan-out paralelo e stalls internos

O `is_alive()` do worker detecta morte do thread, mas não detecta **stall interno** — se `future.result()` em `chat_round.py:463` travar, o worker continua "alive" porém preso.

Política obrigatória:
- Toda chamada `future.result()` deve usar `timeout` explícito.
- Em caso de `TimeoutError`, cancelar o `Future`, enfileirar `RenderEvent("error", ...)` e restaurar o turno.
- O worker deve emitir **heartbeat** periódico via `ui_event_queue` para que o main loop possa detectar inatividade.

```python
try:
    result = future.result(timeout=AGENT_TIMEOUT_SECS)
except TimeoutError:
    future.cancel()
    self._ui_queue.put(RenderEvent("error", "Agente não respondeu no prazo"))
    self._turn_manager.reset()
    return
```

### 4.6 Worker thread resiliente

```python
class ChatWorker:
    def _run(self, msg: str) -> None:
        try:
            result = self._process(msg)
            self._ui_queue.put(RenderEvent("agent_msg", result))
        except UserCancelledError:
            pass
        except Exception as exc:
            logger.exception("chat worker error")
            self._ui_queue.put(RenderEvent("error", str(exc)))
        finally:
            self._turn_manager.reset()   # garante retorno ao turno humano
```

### 4.7 Segundo canal cross-thread: `EventSink` e tasks em background

Mesmo após M3 migrar o fluxo de chat, há um segundo canal que gera UI fora da main thread:

- `app/event_sink.py:40` executa handlers **inline** no thread chamador.
- `app/core.py:448` registra handlers que chamam `show_muted_message` / `show_warning_message`.
- `app/task_repository.py:42` publica eventos de task a partir de threads de executor.

**Consequência:** task executors em background continuam chamando `renderer` fora da main thread, violando o invariante da seção 4.2 mesmo após M3.

**Correção necessária:** `EventSink.publish()` deve enfileirar em `ui_event_queue` em vez de chamar handlers inline quando chamado fora da main thread:

```python
def publish(self, event: str, payload=None):
    if threading.current_thread() is threading.main_thread():
        self._dispatch(event, payload)   # direto — já é main thread
    else:
        self._ui_queue.put(RenderEvent("event", payload, metadata={"event": event}))
```

O `_drain_ui_events()` do main loop trata o caso `kind="event"` chamando `_dispatch` de forma segura.

---

## 5. Contentor de estado compartilhado (`SessionState`)

Em vez de atributos espalhados em `app`, um único objeto thread-safe:

```python
class SessionState:
    """Estado mutável compartilhado entre main thread e workers (read-heavy)."""

    def __init__(self):
        self._lock = threading.RLock()
        self._data: dict = {}

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def update(self, **kwargs):
        with self._lock:
            self._data.update(kwargs)

    # Atalhos tipados
    @property
    def history(self) -> list:
        return self.get("history", [])

    def append_history(self, msg: dict) -> None:
        with self._lock:
            self._data.setdefault("history", []).append(msg)
```

Workers recebem `SessionState` — não `app`.

---

## 6. Interfaces (Protocols)

Cada camada expõe um Protocol, não a classe concreta:

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class IRenderer(Protocol):
    def show_system(self, msg: str) -> None: ...
    def show_agent(self, agent: str, msg: str) -> None: ...
    def show_error(self, msg: str) -> None: ...
    def show_warning(self, msg: str) -> None: ...

@runtime_checkable
class ISessionStorage(Protocol):
    def session_id(self) -> str: ...
    def append_log(self, role: str, content: str) -> None: ...
    def load_last_session(self) -> dict: ...

@runtime_checkable
class IAgentClient(Protocol):
    def call(self, agent: str, prompt: str, history: list) -> str: ...
    def cancel(self) -> None: ...

@runtime_checkable
class IPluginResolver(Protocol):
    def get(self, name: str): ...
    def active_agents(self) -> list[str]: ...
```

---

## 7. Responsabilidade de cada componente

| Componente | Camada | Responsabilidade | Não deve |
|---|---|---|---|
| `TerminalRenderer` | Presentation | Renderizar no terminal; nunca bloquear | Ser chamado de worker threads |
| `InputGate` | Presentation | Ler input do usuário; gerir toolbar | Processar lógica de negócio |
| `EventSink` | Presentation | Distribuir eventos de UI da fila | Produzir eventos |
| `TurnManager` | Domain | Sincronizar turno humano ↔ agente | Ter lógica de negócio |
| `SessionState` | Domain | Estado mutável thread-safe | Ter lógica de negócio |
| `TaskRepository` | Domain | CRUD de tasks; acesso ao DB | Chamar agentes |
| `ChatRoundOrchestrator` | Application | Orquestrar rodada multiagente | Renderizar diretamente |
| `AppDispatchServices` | Application | Chamar AgentGateway; toolloop | Acessar terminal |
| `AppTaskServices` | Application | Gerenciar ciclo de vida de tasks | Acessar storage diretamente |
| `AppSystemLayer` | Application | Processar comandos `/cmd` | Ter estado de sessão |
| `AgentClient` | Infrastructure | Chamar backend do agente | Conhecer UI |
| `PluginRegistry` | Infrastructure | Resolver plugins por nome | Conhecer Application |
| `SessionStorage` | Infrastructure | Persistir histórico e logs | Conhecer renderer |
| `PromptBuilder` | Infrastructure | Montar prompt final | Chamar agentes |

---

## 8. Estrutura de arquivos alvo

```
quimera/
  domain/
    turn.py              # TurnManager (já existe em app/turn.py → mover)
    session_state.py     # SessionState (novo)
    history.py           # lógica de trim/restore de histórico
    task_repository.py   # (mover de app/task_repository.py)
  application/
    chat_round.py        # ChatRoundOrchestrator (já existe → desacoplar de app)
    dispatch.py          # AppDispatchServices (já existe → remover acesso a app)
    task_services.py     # AppTaskServices
    system_layer.py      # AppSystemLayer
    worker.py            # ChatWorker (novo — thread segura)
  presentation/
    renderer.py          # TerminalRenderer (já existe em ui/)
    input_gate.py        # InputGate (já existe em app/)
    event_sink.py        # EventSink + RenderEvent (consumidor do ui_event_queue)
  infrastructure/
    agent_client.py      # AgentClient (já existe em agents/)
    storage.py           # SessionStorage (já existe)
    plugin_registry.py   # PluginRegistry (já existe em plugins/base.py)
    prompt_builder.py    # PromptBuilder (já existe)
  app/
    core.py              # QuimeraApp — apenas composição e ciclo principal (main loop)
    interfaces.py        # Todos os Protocol (IRenderer, IStorage, IAgentClient, ...)
```

---

## 9. Ciclo principal (`run()`) alvo

```python
def run(self):
    self._show_banner()
    ui_queue: queue.Queue[RenderEvent] = queue.Queue()
    worker_pool = ChatWorkerPool(
        session_state=self._session_state,
        agent_client=self._agent_client,
        ui_queue=ui_queue,
        turn_manager=self._turn_manager,
    )
    try:
        while True:
            # 1. Drena eventos de UI produzidos por workers
            self._drain_ui_events(ui_queue)

            # 2. Se não é turno do humano, aguarda (com watchdog no worker)
            if not self._turn_manager.is_human_turn:
                if not worker_pool.is_alive():
                    self._turn_manager.reset()  # worker morreu: recupera
                self._turn_manager.wait_for_human_turn(timeout=0.01)
                continue

            # 3. Lê input (não bloqueante)
            user = self._input_gate.read(timeout=0)
            if user is None:
                if not sys.stdin.isatty():
                    break
                continue

            if user == CMD_EXIT:
                break

            # 4. Comandos de sistema não geram turno de agente
            if self._system_layer.handle(user):
                continue

            # 5. Entrega mensagem para o worker e alterna turno
            worker_pool.submit(user)
            self._turn_manager.next_turn()  # só após submit — sem janela de corrida

    finally:
        worker_pool.shutdown()
        self._session_services.save()
```

**Diferenças críticas em relação ao `run()` atual:**
- `drain_ui_events()` é o único ponto que chama `renderer` no loop.
- `submit()` ocorre **antes** de `next_turn()` — elimina a janela de corrida presente em `core.py:1059`.
- Se o worker morreu, `is_alive()` detecta e `reset()` evita o travamento eterno.

> **Nota:** `is_alive()` detecta morte do worker, mas não detecta **stall interno** (ex: `future.result()` preso no fan-out paralelo de `chat_round.py:463`). Ver seção 4.5.

---

## 10. Plano de migração incremental

As etapas abaixo podem ser feitas independentemente, sem reescrita total:

| Etapa | O que fazer | Critério de saída |
|---|---|---|
| **M1** | Criar `app/interfaces.py` com todos os Protocols | Arquivo existe; nenhum import quebrado |
| **M2** | Extrair `SessionState` de `app` para `domain/session_state.py` | `app.history`, `app.shared_state` passam a delegar para `SessionState` |
| **M3** | Criar `RenderEvent` e `ui_event_queue`; adaptar produtores em `dispatch`, `agent_gateway`, `chat_round` e `event_sink` | `grep -rn "renderer\." app/dispatch.py app/agent_gateway.py app/chat_round.py` retorna zero; `EventSink` usa fila fora da main thread |
| **M4** | Substituir `_process_chat_queue` por `ChatWorker` resiliente com timeout em `future.result()` e heartbeat | Worker sobrevive a exceções; stall no fan-out paralelo gera erro em vez de travamento |
| **M5** | Remover acesso a `app` dos serviços — injetar dependências explícitas | `grep -r "self\.app\." app/` retorna zero resultados |
| **M6** | Mover arquivos para estrutura `domain/application/infrastructure/` | Imports atualizados; testes passando |

Pré-requisito de M5: seções 8 e 9 do TODO.md concluídas (violações de fronteira eliminadas).

---

## 11. O que NÃO mudar

- `TurnManager` — está correto; só precisa ser movido para `domain/`.
- `AgentGateway` — já recebe dependências injetadas; não passa `app`.
- `PluginRegistry` — API pública estável.
- Protocolo multi-agente (handoff) — lógica de negócio, não arquitetura.
- Formato de persistência (`SessionStorage`) — compatibilidade com sessões existentes.
