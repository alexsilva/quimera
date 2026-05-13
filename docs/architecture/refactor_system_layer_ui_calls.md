# Análise: Remoção de chamadas diretas a renderer em system_layer.py

## Inventário completo

### A. Chamadas diretas a `self.app.renderer.show_*()`

| Linha | Código | Método | Contexto | Tipo | Vira evento? |
|-------|--------|--------|----------|------|--------------|
| 93 | `renderer.show_system(message)` | `show_system` | `flush_deferred_messages` — exibe msgs adiadas | UI (plumbing) | não |
| 123 | `renderer.show_system(message)` | `show_system` | `show_system_message` — exibição síncrona | UI (plumbing) | não |
| 151 | `show_newline()` | `show_newline` | `show_muted_message` — nova linha se não for owning thread | UI (plumbing) | não |
| 153 | `show_system_neutral(message)` | `show_system_neutral` | `show_muted_message` — estilo neutro (dim) | UI (plumbing) | não |
| 155 | `renderer.show_system(message)` | `show_system` | `show_muted_message` — fallback se `show_system_neutral` não existe | UI (plumbing) | não |
| 239 | `self.app.renderer.show_warning(...)` | `show_warning` | `_prompt_bool` — valor inválido | UI (input validation) | não |
| 260 | `self.app.renderer.show_warning(...)` | `show_warning` | `_configure_connection_interactively` — driver inválido | UI (input validation) | não |
| 293 | `self.app.renderer.show_warning(...)` | `show_warning` | `_configure_connection_interactively` — JSON inválido | UI (input validation) | não |
| 355 | `self.app.renderer.show_system(...)` | `show_system` | `handle_command` — `/help` | UI (comando) | não |
| 359 | `self.app.renderer.show_system(...)` | `show_system` | `handle_command` — `/agents` | UI (comando) | não |
| 365 | `self.app.renderer.show_warning(...)` | `show_warning` | `handle_command` — `/connect` sem target | UI (comando) | não |
| 377 | `self.app.renderer.show_warning(...)` | `show_warning` | `handle_command` — `/connect` erro config | UI (comando) | não |
| 400 | `self.app.renderer.show_warning(...)` | `show_warning` | `handle_command` — `/disconnect` sem target | UI (comando) | não |
| 404 | `self.app.renderer.show_system(...)` | `show_system` | `handle_command` — `/disconnect` ok | UI (comando) | não |
| 406 | `self.app.renderer.show_warning(...)` | `show_warning` | `handle_command` — `/disconnect` não encontrado | UI (comando) | não |
| 417 | `self.app.renderer.show_system(...)` | `show_system` | `handle_command` — `/reload` | UI (comando) | não |
| 423 | `self.app.renderer.show_warning(...)` | `show_warning` | `handle_command` — `/prompt` sem target | UI (comando) | não |
| 434 | `self.app.renderer.show_system(...)` | `show_system` | `handle_command` — `/reset_state` | UI (comando) | não |
| 441 | `self.app.renderer.show_system(...)` | `show_system` | `handle_command` — `/approve-all` ativado | UI (comando) | não |
| 443 | `self.app.renderer.show_warning(...)` | `show_warning` | `handle_command` — `/approve-all` indisponível | UI (comando) | não |
| 450 | `self.app.renderer.show_system(...)` | `show_system` | `handle_command` — `/approve` pré-aprovado | UI (comando) | não |
| 452 | `self.app.renderer.show_warning(...)` | `show_warning` | `handle_command` — `/approve` indisponível | UI (comando) | não |

**Total: 22 chamadas diretas.**

### B. Chamadas a `self.show_system_message()` (internas ao system_layer)

| Linha | Mensagem | Contexto | Tipo | Vira evento? |
|-------|----------|----------|------|--------------|
| 371 | `"Agente registrado dinamicamente: {target}"` | `/connect` — plugin dinâmico registrado | UI (comando) | não |
| 372 | `"Configurando conexão para {target}"` | `/connect` — início da config | UI (comando) | não |
| 373 | `"Atual: {format_connection_label(...)}"` | `/connect` — conexão atual | UI (comando) | não |
| 394 | `"Conexão ativa para {target}: ..."` | `/connect` — confirmação final | UI (comando) | não |

### C. Chamadas a `self.show_muted_message()` (internas ao system_layer)

| Linha | Mensagem | Contexto | Tipo | Vira evento? |
|-------|----------|----------|------|--------------|
| 170 | `"[task {task_id}] {agent}:\n{text}"` | `show_task_response` — resultado de task | **Domínio** | **sim → TaskEvent** |
| 425 | `_build_prompt_preview_message(target)` | `/prompt` — preview do prompt | UI (comando) | não |

### D. Observação: `show_task_response` é dead code

`show_task_response` (linha 166–170) é definido e testado, mas **não é chamado por nenhum código de produção** em todo o repositório. Apenas o teste `test_show_task_response_uses_strip_and_emits_only_non_empty` o invoca. Isso significa que a mudança para evento teria **impacto zero** em runtime — seria puramente arquitetural/preventiva.

---

## Proposta de eventos

Apenas 1 chamada em `system_layer.py` é candidata a virar evento:

### `show_task_response` → `TaskEvent`

**Atual:**
```python
def show_task_response(self, task_id: int, agent: str, response: str) -> None:
    text = strip_tool_block(response).strip()
    if text:
        self.show_muted_message(f"[task {task_id}] {agent}:\n{text}")
```

**Proposta (via EventSink):**

Em `task_events.py` (a criar):
```python
@dataclass
class TaskEvent:
    task_id: int
    agent: str
    response: str
    event_type: str = "task_result"
```

Em `event_sink.py` (a criar):
```python
class EventSink:
    def __init__(self):
        self._subscribers: dict[type, list[callable]] = {}

    def publish(self, event: object) -> None:
        for callback in self._subscribers.get(type(event), []):
            callback(event)

    def subscribe(self, event_type: type, callback: callable) -> None:
        self._subscribers.setdefault(event_type, []).append(callback)
```

Em `system_layer.py`, `show_task_response` seria substituído por um subscriber registrado em `QuimeraApp.__init__`:
```python
# Em core.py (__init__)
event_sink.subscribe(TaskEvent, self._on_task_event)

# Método em QuimeraApp ou system_layer
def _on_task_event(self, event: TaskEvent) -> None:
    text = strip_tool_block(event.response).strip()
    if text:
        self.system_layer.show_muted_message(
            f"[task {event.task_id}] {event.agent}:\n{text}"
        )
```

### Todas as outras chamadas permanecem como estão

As 21 chamadas restantes a `self.app.renderer.show_*()` e as 6 chamadas a `self.show_system_message()`/`self.show_muted_message()` são **UI pura** — validação de input, feedback de comando slash, notificação de estado. Não carregam semântica de domínio e **não devem virar eventos**.

---

## Risco e esforço

### Linhas que mudariam em `system_layer.py`

| Arquivo | Mudança | Linhas afetadas |
|---------|---------|----------------|
| `system_layer.py` | Remover `show_task_response` (método inteiro) | 5 linhas (166–170) |
| `system_layer.py` | Adicionar `_on_task_event` como subscriber (se colocado aqui) | ~8 linhas |
| `core.py` | Registrar subscriber no `__init__` | +2 linhas |
| `event_sink.py` | Criar classe (novo arquivo) | ~20 linhas |
| `task_events.py` | Criar dataclass (novo arquivo) | ~8 linhas |

**Total estimado: ~40 linhas (nova + alteradas).**

### Testes que quebrariam

| Teste | Arquivo | Impacto |
|-------|---------|---------|
| `test_show_task_response_uses_strip_and_emits_only_non_empty` | `test_app_system_layer.py:160` | **Quebra** — método removido |

Esse teste precisaria ser refatorado para publicar `TaskEvent` no `EventSink` e verificar o efeito colateral no subscriber. Porém, como `show_task_response` nunca é chamado em produção, o risco de regression runtime é **zero** — apenas o teste precisa ser atualizado.

### Risco geral

**Baixíssimo.** A única chamada que viraria evento (`show_task_response`) é dead code. A refatoração em `system_layer.py` é essencialmente cosmética/arquitetural. O ganho real está em refatorar os **serviços de domínio** (`task.py`, `dispatch.py`, `chat_round.py`, `session.py`) que hoje chamam `renderer.show_*()` diretamente — esses sim somam ~25 chamadas.

---

## Plano em fases

### Fase 1: Infraestrutura de eventos (estimativa: ~30 min)

1. Criar `quimera/app/event_sink.py` com a classe `EventSink` (publish/subscribe genérico)
2. Criar `quimera/app/task_events.py` com dataclass `TaskEvent`
3. Instanciar `EventSink` em `QuimeraApp.__init__`
4. Registrar subscriber básico para `TaskEvent` que chama `system_layer.show_muted_message`

### Fase 2: Refatorar `system_layer.py` (estimativa: ~15 min)

1. Remover método `show_task_response`
2. Criar método `_on_task_event` (em `system_layer` ou como closure em `core.py`)
3. Atualizar teste `test_show_task_response_uses_strip_and_emits_only_non_empty` para usar `EventSink`
4. Verificar que `make test` passa

### Fase 3 (opcional): Expandir para serviços de domínio (estimativa: ~2–3h)

Esta fase ataca o problema real das 41+ chamadas. Para cada serviço de domínio:

| Serviço | Chamadas `renderer.show_*()` | Ação |
|---------|-----------------------------|------|
| `chat_round.py` | 6 (show_system, show_warning, show_message, show_handoff) | Publicar `ChatRoundEvent` |
| `dispatch.py` | 2 (show_message, show_no_response) | Publicar `DispatchEvent` |
| `session.py` | 4 (show_system, show_muted_message) | Publicar `SessionEvent` |
| `inputs.py` | 4 (show_system, show_error) | Publicar `InputEvent` |
| `task.py` | 1 (show_warning) | Publicar `TaskCommandEvent` |
| `core.py` | 8 (show_system, show_warning, show_neutral) | Migrar para eventos ou manter (são UI de inicialização) |

Cada evento ganha seu próprio arquivo, e `system_layer.py` ganha subscribers correspondentes que chamam `renderer.show_*()` — mantendo a separação: **domínio publica, UI escuta**.

### Ordem recomendada

```
Fase 1 (infra) → Fase 2 (system_layer) → Fase 3 (chat_round) → (dispatch) → (session) → (inputs) → (task)
```

Cada sub-fase da Fase 3 é independente e pode ser revertida sem afetar as demais.
