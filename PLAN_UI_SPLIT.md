# Plano: Persistent TUI com Input Dock (HSplit)

> Abordagem 1 — `prompt_toolkit Application` com layout HSplit
> Registrado em: 2026-06-27

## Arquitetura alvo

```
prompt_toolkit Application (main thread, asyncio loop)
├── HSplit
│   ├── output_window  — scrolling, FormattedTextControl, auto-scroll to bottom
│   ├── separator_window — 1 linha "─"
│   └── bottom_pane (DynamicContainer)
│       ├── IDLE: TextArea (input livre, history, completions) + toolbar
│       └── AWAITING_INPUT: overlay com question + single-line input
```

---

## Passo A — `application.py` atrás de flag `--ui=split`

**Arquivos:** novo `quimera/ui/application.py`, `quimera/cli.py` (add `--ui`), `quimera/app/core.py`

Cria `QuimeraApplication` com layout HSplit completo, mas sem nada conectado ainda. Flag `--ui=classic` mantém comportamento 100% atual. Todos os testes existentes passam sem modificação.

```python
class QuimeraApplication:
    def run(self) -> None: ...                           # bloqueia main thread (app.run())
    def append_output(self, ansi_text: str) -> None: ... # thread-safe
    def invalidate(self) -> None: ...                    # call_soon_threadsafe
    def request_approval(...) -> str | None: ...         # bloqueia consumer thread
    def request_ask_user(...) -> str | None: ...
    def get_loop(self) -> asyncio.AbstractEventLoop | None: ...
```

---

## Passo B — Saída do compositor no output pane

**Arquivos:** `quimera/ui/compositor.py`, `quimera/ui/application.py`

- `TerminalCompositor` ganha `set_app_sink(sink)`
- Dentro de `_cprint()` (já no writer thread), renderiza o mesmo `renderable` em um `Console(file=StringIO(), force_terminal=True)` e chama `sink.push_ansi(rendered_ansi)`
- `push_ansi` faz `ANSI(text)` → fragmentos → `self._output_lines.extend(...)` → `app.invalidate()`
- Eventos transient (`TransientWindowEvent`, `OutputControlEvent`) viram no-op quando `_app_sink` está set — progresso aparece diretamente no output pane

**Decisão de design:** usar `ANSI()` (não strip+rerender) — zero reimplementação, fidelidade total.

---

## Passo C — Input do dock → sessão

**Arquivos:** `quimera/app/core.py`, `quimera/app/chat_processor.py`

- `submit_fn` passado ao `QuimeraApplication` coloca o texto no `chat_queue` existente
- `run_chat_loop_split` chama `qapp.run()` no main thread; `ChatWorker` continua em thread separada

```python
def run_chat_loop_split(app, qapp: QuimeraApplication, ...) -> None:
    chat_worker.start()
    qapp.run()          # bloqueia até quit
    # shutdown idêntico ao run_chat_loop atual
```

---

## Passo D — Overlay de approval / ask_user

**Arquivos:** `quimera/app/input_broker.py`, `quimera/ui/application.py`

- `InputBroker` ganha `set_qapp(qapp)`
- Em `_handle_approval()` e `_handle_ask_user()`, quando `_qapp is not None`, delega para `_qapp.request_approval()` / `_qapp.request_ask_user()` em vez de `gate.read_*_in_terminal()`

Máquina de estados no bottom pane:
```
IDLE → (InputBroker envia request) → AWAITING_INPUT
     → (Enter ou Ctrl-C) → resolve/cancel → IDLE
```

`_PendingPromptRequest` usa `threading.Event` para o consumer thread bloquear até resposta ou timeout.

---

## Passo E — Tornar split o padrão; remover floor logic

Após validação dos passos A–D:
- `--ui` default muda para `"split"`
- Remove de `quimera/ui/renderer.py`: `_request_floor`, `_release_floor`, `approval_window`, `input_window`, `selection_window`, `terminal_floor`
- Remove de `quimera/ui/compositor.py`: processamento de `TransientWindowEvent`/`TransientClearEvent`/`OutputControlEvent` quando em split mode
- `PromptSession` em `quimera/app/prompt_input.py` vira apenas provider de history/completions para o `Buffer` do `QuimeraApplication`

---

## Testes novos

| Grupo | Cobertura |
|---|---|
| `tests/test_ui_application.py` | layout HSplit, output buffer (thread-safety, cap 10k linhas), máquina de estados overlay, key bindings |
| `tests/test_input_broker_split.py` | roteamento via `_qapp`, timeout retorna `None`, approval/ask_user delegates corretos |
| Updates em `tests/test_ui.py` | `set_app_sink`, no-op de TransientWindowEvent com sink set |

---

## Ordem de execução e risco

| Passo | Risco | Mitigação |
|---|---|---|
| A | Zero | Flag esconde tudo, testes existentes inalterados |
| B | Médio — dupla escrita stdout | `force_terminal=True` + StringIO isola Rich do terminal real |
| C | Baixo — `chat_queue` já existe | `run_chat_loop_split` é nova função, não altera a atual |
| D | Médio — `threading.Event` timeout | Mesmo mecanismo de timeout já usado no broker |
| E | Alto — remove floor logic | Só executar após D estável com `--ui=split` por padrão em staging |

---

## Dependências externas

- `prompt_toolkit >= 3.0` (já presente)
- `rich` com `ANSI()` parser (já presente em pt 3.x)

---

## Ponto de partida sugerido

Começar pelo **Passo A**: criar `quimera/ui/application.py` com o layout HSplit completo e adicionar `--ui=split|classic` ao CLI. Nenhuma funcionalidade existente é alterada.
