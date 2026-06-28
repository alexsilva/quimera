# Passo F — Injeção de prompt em agente ativo via stdin

## Contexto

Com o split UI como único modo (Passo E), o input dock é persistente. O usuário
pode digitar enquanto um agente CLI roda. Hoje, `submit_fn` sempre coloca o texto
em `_split_q`, e o chat loop só o processa na **próxima rodada** — mesmo com
agente ativo, o texto fica enfileirado. O objetivo é **injetar diretamente no
stdin do processo do agente** quando ele estiver em execução.

## Problemas arquiteturais

### 3a. stdin é fechado após escrita inicial

Em `agents/client.py:406-410`, após `proc.stdin.write(input_text)` e
`proc.stdin.flush()`, o código faz `proc.stdin.close()`. Depois disso, não é
possível injetar mais dados.

### 3b. Referência ao processo ativo não chega na TUI

`AgentClient._current_proc` (`client.py:365`) aponta para o subprocesso ativo, mas
não há mecanismo para expor `proc.stdin` à `QuimeraApplication` (TUI thread).

### 3c. Nem todo agente CLI aceita input contínuo no stdin

Agentes como **OpenCode** lêem stdin até EOF e processam em lote; não esperam
input adicional. Para injetar durante execução, o agente precisa explicitamente
suportar leitura incremental.

### 3d. Warm pool (novo — não estava no plano original)

Em `client.py:347-349`, quando `_primed_proc` é reutilizado, o processo vem do pool
e seu stdin pode já ter sido escrito e fechado em invocações anteriores.
`proc.stdin.closed == True` nesses casos. A lógica de `keep_stdin_open` deve
detectar isso e não tentar escrever num pipe fechado.

## Arquitetura proposta

```
TUI thread (QuimeraApplication)          Chat thread (QuimeraApp / AgentClient)
┌─────────────────────────────┐          ┌──────────────────────────────────┐
│ _on_submit(buffer)         │          │ AgentClient.run()                 │
│  ├─ _inject_fn ≠ None?     │          │  ├─ proc = subprocess.popen_text()│
│  │  ├─ Sim → inject(text)  │◄─stdin──►│  ├─ proc.stdin (keep_open=True)   │
│  │  └─ Não → submit_fn()   │          │  ├─ _current_proc = proc          │
│  │                         │          │  └─ active_stdin property (lazy)   │
│  └─ toolbar: agente ativo  │          └──────────────────────────────────┘
└─────────────────────────────┘

split_q queue (chat loop normal)         proc.stdin (injeção direta)
```

### Decisão: flag de perfil + inject_fn

A injeção via stdin **deve ser opt-in** por agente. Perfis que suportam ganham
`keep_stdin_open = True` na `CliConnection`. Por padrão, o comportamento é o atual.

`prompt_as_arg` e `keep_stdin_open` são **ortogonais**: um controla como o prompt
inicial chega (arg vs stdin), o outro controla se o pipe permanece aberto para
injeções subsequentes. Ambos `True` é válido — prompt inicial via arg, pipe aberto
para injeções durante execução.

## Implementação

### F1. CliConnection: flag `keep_stdin_open`

**Arquivo:** `quimera/profiles/base.py`

```python
@dataclass
class CliConnection:
    cmd: List[str] = field(default_factory=list)
    prompt_as_arg: bool = False
    output_format: Optional[str] = None
    env: Optional[dict] = None
    cwd: Optional[str] = None
    keep_stdin_open: bool = False   # True = não fecha stdin após prompt; opt-in por perfil
```

Perfis OpenCode que suportam stdin contínuo ativam `keep_stdin_open=True` na
connection correspondente.

### F2. AgentClient: não fechar stdin quando flag ativa

**Arquivo:** `quimera/agents/client.py`

Em `client.py:405-410`, substituir:

```python
# ANTES
if input_text and proc.stdin:
    proc.stdin.write(input_text)
    proc.stdin.flush()
if proc.stdin:
    proc.stdin.close()
```

Por:

```python
# DEPOIS
if input_text and proc.stdin and not proc.stdin.closed:
    proc.stdin.write(input_text)
    proc.stdin.flush()

_keep_open = False
if agent:
    from ..profiles import get_profile          # import local para evitar ciclo
    profile = get_profile(agent)
    conn = getattr(profile, 'connection', None) or getattr(profile, 'effective_connection', lambda: None)()
    _keep_open = getattr(conn, 'keep_stdin_open', False)

if not _keep_open and proc.stdin and not proc.stdin.closed:
    proc.stdin.close()
```

**Nota sobre warm pool**: quando `_primed_proc` é reutilizado, `proc.stdin.closed`
já é `True` (pipe fechado pelo run anterior). O guard `not proc.stdin.closed`
evita tentar escrever num pipe morto. O pool gerrencia o ciclo de vida do processo;
`_active_stdin` no `finally` limpa a referência ao encerrar.

Adicionar property para expor stdin ativo (verificação por `poll()`, mais confiável
que `_agent_running` que pode ser False antes do cleanup de `_current_proc`):

```python
@property
def active_stdin(self):
    """Retorna stdin do processo ativo, ou None se nenhum agente rodando."""
    proc = self._current_proc
    if proc is not None and proc.poll() is None:
        stdin = getattr(proc, 'stdin', None)
        if stdin is not None and not stdin.closed:
            return stdin
    return None
```

No `finally` de `run()`, limpar `_active_stdin` implicitamente zerando `_current_proc`:

```python
# finally de run() — já existente
self._current_proc = None   # active_stdin retorna None automaticamente
```

### F3. QuimeraApp: expor `active_agent_stdin`

**Arquivo:** `quimera/app/core.py`

```python
@property
def active_agent_stdin(self):
    """Property lazy — consultada em runtime, nunca stale."""
    client = getattr(self, 'agent_client', None)
    if client is not None:
        return client.active_stdin
    return None
```

**Arquivo:** `quimera/cli.py` — dentro do bloco split UI, **após** `app = QuimeraApp(...)`
e **antes** de `qapp = QuimeraApplication(...)`. O closure captura `app` após
construção completa (incluindo `app.agent_client`) — sem risco de stale.

```python
def _inject(text: str) -> bool:
    """Tenta injetar texto no stdin do agente ativo. Retorna True se conseguiu."""
    stdin = app.active_agent_stdin      # lazy — consulta em runtime
    if stdin is not None:
        try:
            stdin.write(text + "\n")
            stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            # OSError: pipe quebrado; ValueError: TextIOWrapper fechado;
            # AttributeError: race entre check e acesso
            pass
    return False

qapp = QuimeraApplication(
    submit_fn=_submit,
    inject_fn=_inject,          # NOVO
    toolbar_context_resolver=_toolbar_resolver,
    ...
)
```

### F4. QuimeraApplication: `_on_submit` com rota de injeção

**Arquivo:** `quimera/ui/application.py`

Adicionar `inject_fn` ao construtor:

```python
def __init__(
    self,
    *,
    submit_fn=None,
    inject_fn=None,     # NOVO
    toolbar_context_resolver=None,
    ...
):
    self._inject_fn = inject_fn
    ...
```

Modificar `_on_submit` (atual: `application.py:407-412`):

```python
def _on_submit(self, buffer) -> None:
    text = buffer.text.strip()
    if not text:
        return
    if text == CMD_EXIT:
        if self._app is not None:
            try:
                self._app.exit()
            except Exception:
                pass
        if self._submit_fn is not None:
            self._submit_fn(text)
        return

    self.append_output(f"\033[1;36m>>> \033[0m{text}\n")

    injected = False
    if self._inject_fn is not None:
        try:
            injected = self._inject_fn(text)
        except Exception:
            pass

    if injected:
        # Texto já foi para stdin do agente — não bloqueia o input
        self._focus_input_area()
    else:
        # Fallback: fila normal (agente inativo ou não suporta injeção)
        self._awaiting_response = True
        if self._submit_fn is not None:
            self._submit_fn(text)

    self.invalidate()
```

### F5. Toolbar: indicar agente ativo e modo "injetar"

**Arquivo:** `quimera/ui/application.py` — `_get_toolbar_text`

```python
# Novo atributo no __init__
self._agent_label: str | None = None

# Método para atualizar externamente (chamado pelo chat loop via callback)
def set_active_agent(self, agent_name: str | None) -> None:
    self._agent_label = agent_name
    self.invalidate()

# Em _get_toolbar_text, antes do check de _awaiting_response
if self._agent_label:
    return FormattedText([
        ("", " "),
        ("class:toolbar.btn.accent", f" ⟳ {self._agent_label} "),
        ("", "  "),
        ("class:toolbar.btn.dim", " Enter: injetar  Ctrl+Q: sair "),
    ])
```

Para notificar a TUI, `AgentClient.call()` (ou `run()`) chama um callback opcional:

```python
# Em AgentClient.__init__
self._on_agent_active: Callable[[str | None], None] | None = None

# Em run(), antes de iniciar stream
if self._on_agent_active:
    self._on_agent_active(agent)

# Em run() no finally
if self._on_agent_active:
    self._on_agent_active(None)
```

### F6. Adaptação do agente CLI (ex: OpenCode)

Para que a injeção funcione, o agente CLI precisa:

1. **Não tratar EOF do stdin como fim de conversa**
2. **Ler novas linhas do stdin durante execução** (polling entre tool calls)
3. **Interpretar texto injetado como continuação ou novo comando**

Isso é **fora do escopo do Quimera**. Para agentes que não suportam, `keep_stdin_open`
fica `False` e o comportamento é o atual (fila).

#### Exemplo conceitual (modificação no agente externo)

```python
# No loop principal do agente CLI externo:
while True:
    prompt = sys.stdin.readline()
    if not prompt:
        break
    result = process_prompt(prompt.strip())
    sys.stdout.write(result)
    sys.stdout.flush()
```

### F7. Tratamento de concorrência e shutdown

**Sem lock explícito** — GIL garante atomicidade de leituras de atributos. A janela de
race é coberta por tratamento de exceção.

Exceções capturadas em `inject_fn` e `_on_submit`:
- `OSError` — pipe quebrado (processo morreu)
- `ValueError` — `TextIOWrapper` fechado (fechado pelo outro lado ou por `close()`)
- `AttributeError` — race entre leitura de `_current_proc` e limpeza no finally

Comportamento em cada cenário:

| Cenário | Comportamento |
|---|---|
| Agente terminou enquanto usuário digitava | `inject_fn` captura exceção → retorna False → fallback para fila |
| `_primed_proc` com stdin já fechado | `active_stdin` retorna None (`stdin.closed`) → inject_fn retorna False |
| Usuário injeta sem agente ativo | `active_stdin` retorna None → fallback para fila normal |
| `_current_proc` virou None (finally) | `active_stdin` retorna None imediatamente |

No `finally` de `run()`, `_current_proc = None` é suficiente — `active_stdin`
usa `proc.poll()` no objeto capturado, que retorna não-None quando morto.

### F8. Testes

| Teste | O que verifica |
|---|---|
| `test_inject_stdin_active` | `inject_fn` escreve no pipe quando `active_stdin ≠ None` |
| `test_inject_fallback_queue` | `submit_fn` é chamado quando `active_stdin` é None |
| `test_stdin_not_closed_keep_open` | `proc.stdin.close()` não é chamado quando `keep_stdin_open=True` |
| `test_stdin_closed_normal` | `proc.stdin.close()` ainda é chamado quando `keep_stdin_open=False` |
| `test_inject_after_agent_dead` | `inject_fn` captura exceção e retorna False silenciosamente |
| `test_inject_value_error` | `inject_fn` captura `ValueError` (TextIOWrapper fechado) |
| `test_inject_prompt_as_arg_keep_open` | Com `prompt_as_arg=True + keep_stdin_open=True`, stdin não é fechado |

## Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Agente não trata stdin contínuo | `keep_stdin_open=False` por padrão; opt-in explícito por perfil |
| Race: TUI escreve enquanto chat fecha stdin | `inject_fn` captura `(OSError, ValueError, AttributeError)` |
| Warm pool: stdin já fechado no reuso | Guard `not proc.stdin.closed` antes de escrever |
| `_agent_running` False antes de cleanup | `active_stdin` usa `proc.poll() is None`, não `_agent_running` |
| stdin pipe bloqueia em write grande | Injeções são curtas (prompts de usuário); risco baixo |
| Toolbar confusa sem agente ativo | `set_active_agent(None)` no finally restaura toolbar normal |

## Dependências

- Passos A–E concluídos (split UI é o único modo) ✓
- Perfil do agente-alvo precisa de `keep_stdin_open=True` + leitura contínua de stdin
  no CLI externo — fora do escopo do Quimera, documentado como requisito de integração

## Próximos passos após F

1. Adaptar perfil OpenCode com `keep_stdin_open=True` e leitura incremental de stdin
2. Validar com teste interativo: `python quimera.py --agents opencode` + digitar durante execução
3. Considerar `os.write(fd, data)` não-bloqueante se escrita bloquear em prompts grandes
