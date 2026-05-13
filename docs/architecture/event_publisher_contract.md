# Contrato de Publicação de Eventos de Domínio

> **Propósito**: Especificar quem publica cada `TaskEvent`, em qual método, sob qual condição, e como o `EventSink` é injetado.
>
> Resposta ao gap da Seção 14: _"itens 1-3 criam eventos/sink/observer, mas ninguém especifica QUEM chama sink.publish()."_

---

## 1. Matriz Publisher × Evento

A publicação acontece **dentro dos métodos do `TaskRepository`**, pois ele é o ponto único de mutação atômica do banco. Abaixo, a matriz completa:

### 1.1 TaskRepository (`quimera/app/task_repository.py`)

| Método | Transição (estado) | Evento publicado | Condição |
|---|---|---|---|
| `create_task` | — (inserção) | `TaskProposed` | quando `status == TaskStatus.PROPOSED` |
| `create_task` | — (inserção) | `TaskStarted` | quando `status == TaskStatus.IN_PROGRESS` e `assigned_to` preenchido |
| `create_task` | — (inserção) | _(nenhum)_ | quando `status == TaskStatus.PENDING` (default) |
| `transition_task` | `* → APPROVED` | `TaskApproved` | `to_status == TaskStatus.APPROVED` e `can_transition` OK |
| `transition_task` | `* → REJECTED` | `TaskRejected` | `to_status == TaskStatus.REJECTED` e `can_transition` OK |
| `claim_task` | `PENDING\|APPROVED → IN_PROGRESS` | `TaskStarted` | sempre (reserva atômica) |
| `claim_review_task` | `PENDING_REVIEW → REVIEWING` | `TaskReviewStarted` | sempre (reserva atômica) |
| `fail_task` | `* → FAILED` | `TaskFailed` | delega a `transition_task` |
| `submit_for_review` | `IN_PROGRESS → PENDING_REVIEW` | `TaskSubmittedForReview` | sempre que `can_transition` OK |
| `complete_task` | `IN_PROGRESS \| REVIEWING → COMPLETED` | `TaskCompleted` | sempre que `can_transition` OK |
| `requeue_task` | `IN_PROGRESS → PENDING` | `TaskRequeued` | sempre que `can_transition` OK |
| `requeue_task_after_review` | `REVIEWING → PENDING` | `TaskRequeued` | sempre que `can_transition` OK |

### 1.2 TaskRunner (`quimera/runtime/task_runner.py`) — fluxo indireto

| Método | Ação no repositório | Evento resultante |
|---|---|---|
| `run` (resposta None) | `requeue_task` _ou_ `fail_task` | `TaskRequeued` _ou_ `TaskFailed` |
| `run` (sucesso s/ review) | `complete_task` | `TaskCompleted` |
| `run` (sucesso c/ review) | `submit_for_review` | `TaskSubmittedForReview` |
| `run` (exceção) | `requeue_task` _ou_ `fail_task` | `TaskRequeued` _ou_ `TaskFailed` |

Todos os eventos fluem das chamadas de repositório. O `TaskRunner` **não** precisa chamar `sink.publish()` diretamente — desde que o `EventSink` esteja injetado no `TaskRepository`.

### 1.3 TaskReviewer (`quimera/runtime/task_reviewer.py`) — fluxo indireto

| Método | Ação no repositório | Evento resultante |
|---|---|---|
| `review` (sucesso) | `complete_task(reviewed_by=…)` | `TaskCompleted` |
| `review` (rejeitado) | `requeue_task_after_review` | `TaskRequeued` |
| `review` (cancelado) | `fail_task` | `TaskFailed` |
| `review` (exceção s/ fallback) | `fail_task` | `TaskFailed` |
| `review` (auto-review) | `transition_task(→PENDING_REVIEW)` | ⚠️ **gap** (ver §3) |
| `review` (exceção c/ fallback) | `transition_task(→PENDING_REVIEW)` | ⚠️ **gap** (ver §3) |

### 1.4 AppTaskServices.handle_task_command (`quimera/app/task.py:261`)

| Método | Ação | Evento |
|---|---|---|
| `handle_task_command` | `repo.create_task(status="pending")` | _(nenhum)_ — task nasce `PENDING`, não `PROPOSED` |

---

## 2. Estratégia de Injeção do EventSink

**Padrão**: Constructor injection com default `None` para compatibilidade retroativa.

### TaskRepository

```python
class TaskRepository:
    def __init__(self, db_path: str, event_sink: EventSink | None = None):
        self.db_path = db_path
        self._event_sink = event_sink
        self._init_db()
```

Dentro de cada método, antes do `return` / `commit` bem-sucedido:

```python
if self._event_sink is not None:
    self._event_sink.publish(event)
```

### TaskRunner (assinantes, não publishers)

Caso o `TaskRunner` ou `TaskReviewer` precise publicar eventos que **não passam pelo repositório** (ex.: `REVIEWING→PENDING_REVIEW` sem repositorio), recebem `EventSink` opcional também.

### Cadeia de construção (AppTaskServices)

Em `quimera/app/task.py`, os builders privados recebem o `EventSink` do `app`:

```python
def _build_task_repository(self) -> TaskRepository:
    return TaskRepository(
        db_path=self.app.tasks_db_path,
        event_sink=getattr(self.app, "event_sink", None),
    )
```

Nenhuma assinatura de método público muda. Testes existentes que não passam `event_sink` continuam funcionando (default `None` → sem publicação).

---

## 3. Eventos Faltantes (Gaps)

### Gap 1: `REVIEWING → PENDING_REVIEW` — sem evento

**Ocorre em** `TaskReviewer.review()`:
- Linha 90: quando o mesmo agente que executou tenta revisar → `transition_task(task_id, PENDING_REVIEW)`
- Linha 180: fallback de exceção → `transition_task(task_id, PENDING_REVIEW)`

**Problema**: A task é devolvida para `PENDING_REVIEW` (outro revisor poderá pegá-la), mas nenhum evento notifica isso. Um observador (ex.: dashboard) perderia o rastro.

**Solução**: Criar `TaskReviewReassigned(task_id, job_id, reason, previous_reviewer)` e publicá-lo no `TaskReviewer.review()` diretamente (já que o repositório não sabe o motivo semântico do revert).

### Gap 2: `PENDING → PENDING` (criação) — sem evento

**Ocorre em** `AppTaskServices.handle_task_command()` + `runtime/tasks.py`.

**Contexto**: `create_task` com `status="pending"` (default) não mapeia para `TaskProposed`, que semanticamente corresponde a `PROPOSED`. Tasks nascem `PENDING` no fluxo `handle_task_command`.

**Solução**: Esta é uma escolha de design. Duas opções:
  1. (Mais puro) Criar evento `TaskCreated` para representar nascimento em `PENDING`.
  2. (Menos invasivo) Aceitar que tasks `PENDING` não disparam evento de criação. Eventos só disparam quando há mudança de estado detectável.

**Recomendação**: Opção 2 por ora — `PENDING` é estado inicial default. Se um subscriber precisar saber de novas tasks, pode usar o evento `TaskStarted` (quando a task é reivindicada) ou assinar `PENDING` via polling.

### Gap 3: `update_task` — sem estado semântico

`update_task` faz `UPDATE` direto sem `can_transition`. É chamado pelo CLI legado e testes. Não publica evento.

**Solução**: Nenhuma. O método é raw e será eliminado quando a migração para `transition_task` for completa.

### Gap 4: `release_agent_tasks` — sem evento

Reseta tasks de um agente falho para `PENDING`. Não publica evento.

**Solução**: Nenhuma por ora. Se necessário, publicar `TaskRequeued` para cada task afetada.

---

## 4. Cobertura vs. State Machine

Para cada transição em `VALID_TRANSITIONS`:

| Transição | Evento | Coberto por |
|---|---|---|
| `PROPOSED → APPROVED` | `TaskApproved` | `transition_task` |
| `PROPOSED → REJECTED` | `TaskRejected` | `transition_task` |
| `APPROVED → IN_PROGRESS` | `TaskStarted` | `claim_task` |
| `PENDING → IN_PROGRESS` | `TaskStarted` | `claim_task` |
| `IN_PROGRESS → PENDING_REVIEW` | `TaskSubmittedForReview` | `submit_for_review` |
| `IN_PROGRESS → COMPLETED` | `TaskCompleted` | `complete_task` |
| `IN_PROGRESS → FAILED` | `TaskFailed` | `fail_task` |
| `IN_PROGRESS → PENDING` | `TaskRequeued` | `requeue_task` |
| `PENDING_REVIEW → REVIEWING` | `TaskReviewStarted` | `claim_review_task` |
| `REVIEWING → COMPLETED` | `TaskCompleted` | `complete_task` |
| `REVIEWING → FAILED` | `TaskFailed` | `fail_task` |
| `REVIEWING → PENDING` | `TaskRequeued` | `requeue_task_after_review` |
| **`REVIEWING → PENDING_REVIEW`** | ⚠️ **NENHUM** | **gap** — TaskReviewer |
| `COMPLETED → ∅` | _(terminal)_ | — |
| `FAILED → ∅` | _(terminal)_ | — |
| `REJECTED → ∅` | _(terminal)_ | — |

---

## 5. Ordem de Implementação

| Passo | O quê | Risco | Justificativa |
|---|---|---|---|
| **1** | `EventSink` + eventos em `TaskRepository` | Baixo | Publicação é aditiva. `event_sink=None` por default. Nenhum teste quebra. Nenhum subscriber precisa existir. |
| **2** | Evento `TaskReviewAssigned` + publish em `TaskReviewer` (gap `REVIEWING→PENDING_REVIEW`) | Baixo | Apenas o `TaskReviewer` precisa do `EventSink` diretamente. |
| **3** | Injetar `EventSink` em `AppTaskServices._build_*` | Baixo | Apenas adiciona arg opcional aos builders. |
| **4** | Adicionar subscribers (logging, métricas, dashboard) | Médio | Depende dos requisitos de downstream. |
| **5** | Remover default `None` (opcional) | Alto | Só depois de todos os callers atualizados. |

**Risco de regressão**: Próximo de zero enquanto `event_sink=None` for aceito. Testes existentes em `tests/test_task_repository.py` usam `TaskRepository(db_path)` sem sink — continuam passando.

---

## Apêndice: Diagrama de Fluxo

```
AppTaskServices
  └─ handle_task_command
       └─ repo.create_task(status=PENDING)  →  (sem evento por design)

TaskRunner.run
  ├─ repo.fail_task(...)                    →  TaskFailed      ✔
  ├─ repo.requeue_task(...)                 →  TaskRequeued    ✔
  ├─ repo.submit_for_review(...)            →  TaskSubmitted   ✔
  └─ repo.complete_task(...)                →  TaskCompleted   ✔

TaskReviewer.review
  ├─ repo.requeue_task_after_review(...)    →  TaskRequeued    ✔
  ├─ repo.complete_task(...)                →  TaskCompleted   ✔
  ├─ repo.fail_task(...)                    →  TaskFailed      ✔
  └─ repo.transition_task(→PENDING_REVIEW)  →  ⚠️  GAP (ver §3)

runtime/tasks.py CLI
  ├─ propose_task(...)                      →  TaskProposed    ✔
  ├─ approve_task(...)                      →  TaskApproved    ✔
  ├─ reject_task(...)                       →  TaskRejected    ✔
  └─ demais → repo.*                       →  eventos acima   ✔
```
