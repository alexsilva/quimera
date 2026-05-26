# Change Log

## Unreleased
- API Public: Stabilize QuimeraApp public surface. Expose only essential interfaces via quimera.app.__init__: QuimeraApp, logger, and PromptAwareStderrHandler. No breaking changes expected relative to previous public interface (item 5).
- Compatibility: API surface reviewed; public surface remains stable with no breaking changes anticipated.
- Document any future breaking changes here to aid consumers.

## [1b9dc63] fix(renderer): stabilize output suspend/resume during editor (PR-10)
- `renderer.py`: suspensão imediata antes de enfileirar evento de controle, eliminando janela de corrida que permitia mensagens de agentes vazarem durante `/context edit`.
- `editor.py`: `resume_output()` garantido mesmo com timeout de ack.
- Testes: `test_ui.py` e `test_context_manager.py` com cobertura de regressão.

## [e72258d] refactor(core): extract tty control from app and chat loop
- `tty_control.py` extraído com lógica de suspend/resume de TTY.
- `chat_processor.py` slim: loop de chat usa `tty_control` em vez de inline.

## [4f366fc] refactor(core): extract ToolbarManager, remove _BACKWARD_MAP, migrate tests
- `toolbar.py` (`ToolbarManager`) extraído de `core.py`.
- `_BACKWARD_MAP` e branches de compatibilidade em `__getattr__`/`__setattr__` removidos.
- Testes migrados de `app._attr` para `app.runtime_state.attr`.
- `InputGate.is_active()` promovido a fonte primária de estado de prompt ativo; fallback legado `nonblocking_input_status` removido de `_redisplay_user_prompt_if_needed`.

## [80bf09a] refactor(bug-services): rename AppBugServices to BugServices, flatten ChatProcessor (PR-9)
- `app_bug_services.py` → `bug_services.py` (`AppBugServices` → `BugServices`).
- `ChatProcessor` achatado: responsabilidade de handoff/roteamento centralizada.
- Regressão em `system_layer.py` e `handlers.py` corrigida com fallback explícito para app legado sem `runtime_state`.

## [fde8c4f] refactor(core): extract bug services and command router
- `bug_services.py` e `command_router.py` extraídos de `core.py`.

## [bb427d7] refactor(core): extract ui_event_handler and stabilize renderer during editor (PR-8)
- `ui_event_handler.py` (`UIEventHandler`) extraído.
- Renderer estabilizado durante abertura de editor externo.

## [7977585] refactor(core): extract chat_processor and slim run orchestration (PR-7)
- `chat_processor.py` (`ChatProcessor`) extraído de `core.py`.
- Loop `run()` reduzido; orquestração de rodada delegada ao processor.

## [e59c87b] refactor(core): extract session_bootstrap and remove resolve path wrappers (PR-6)
- `session_bootstrap.py` extraído com lógica de inicialização de sessão (paths, debug, análise de bugs anteriores).
- Métodos `_resolve_*path` delegadores removidos de `core.py`.
