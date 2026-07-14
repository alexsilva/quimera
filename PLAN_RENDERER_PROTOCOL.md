# PLAN: Contrato formal de Renderer (fim do capability-sniffing)

Autor: CLAUDE-FABLE · Data: 2026-07-14 · Branch alvo: `main-ui`

## Diagnóstico

Não existe contrato completo de renderer. `IRenderer` (`app/interfaces.py:11`)
declara só 4 métodos (`show_system`, `show_agent`, `show_error`, `show_warning`),
mas os consumidores usam ~25 capacidades opcionais e cada um re-adivinha o que o
renderer sabe fazer via `getattr(renderer, "método", fallback)`:

- ~45 sniffs em 17 arquivos de produção. Maiores: `app/display_service.py` (10),
  `app/dispatch.py` (5), `app/chat_processor.py` (5), `app/ui_event_handler.py` (4),
  `app/chat_round.py` (4), `agents/client.py` (3), `runtime/input_broker.py` (3).
- Métodos mais sniffados: `flush` (7), `show_system_neutral` (4),
  `supports_agent_feed` (3), `notify_agent_failover` (3), `show_delegation` (2),
  `notify_agent_retry` (2), `set_summarizing` (2), `flush_quick` (2), e mais
  ~15 com 1 ocorrência (`show_banner`, `signal_restore_history`,
  `show_prompt_preview`, `update_agent_transient`, `commit_agent_stream`, …).

Consequências: cada renderer novo (ex.: `TextualRenderer`) precisa descobrir o
contrato lendo os chamadores; typos em nomes de método falham silenciosamente
caindo no fallback; e o fallback varia por chamador (às vezes `show_system`,
às vezes no-op), gerando comportamento inconsistente.

## Princípio do redesenho

**Uma classe base concreta `RendererBase` com implementações no-op/fallback
padrão para toda capacidade opcional.** Chamador confia no contrato e chama
direto; quem quer o comportamento sobrescreve. `IRenderer` (Protocol) continua
existindo para tipagem, ampliado para o contrato mínimo real.

Regra de fallback única, decidida na base (não no chamador):
- Capacidades de exibição opcionais (`show_banner`, `show_system_neutral`,
  `show_delegation`, …) → delegam para `show_system` na base.
- Capacidades de infraestrutura (`flush`, `flush_quick`, `signal_restore_history`,
  `set_summarizing`, `set_prompt_integration`, `log_debug_event`, …) → no-op.
- Capacidades booleanas (`supports_agent_feed`) → atributo de classe `False`.
- Acesso a internals (`_audit_logger`, `_agent_window_controller`,
  `_window_manager`) → **não entram no contrato**; esses sniffs indicam vazamento
  de abstração e serão tratados caso a caso na fase 4.

## Fases

### Fase 0 — Caracterização
Inventário exato (arquivo:linha → método sniffado → fallback usado) e teste de
contrato: `RendererBase()` instanciável, todo método do inventário presente,
displays opcionais delegando a `show_system`. Nenhuma mudança de produção.

### Fase 1 — `RendererBase` + herança dos renderers reais
- Criar `quimera/ui/base.py` com `RendererBase`.
- `TerminalRenderer(RendererBase)` e `TextualRenderer(RendererBase)`.
- Ampliar `IRenderer` com o contrato mínimo consolidado.
- Sem mudar chamadores ainda — suíte deve passar inalterada.

### Fase 2 — Fakes de teste herdam a base
32 classes `*Renderer` em `tests/` passam a herdar `RendererBase` (os `Mock()`
não precisam de mudança). Mecânico; destrava o flip das fases 3–4.

### Fase 3 — Flip dos call sites em `app/`
Substituir `getattr(renderer, "x", fallback)` por chamada direta em
`chat_round.py`, `chat_processor.py`, `display_service.py`, `dispatch.py`,
`ui_event_handler.py`, `session.py`, `agent_gateway.py`, `toolbar*.py`,
`inputs.py`, `agent_run_events.py`, `core_facade.py`, `bootstrap/wiring.py`.
Um commit por grupo coeso.

### Fase 4 — Flip fora de `app/` + vazamentos de abstração
`agents/client.py`, `runtime/input_broker.py`, `runtime/approval.py`,
`spy_output_presenter.py`. Sniffs de internals (`_audit_logger` etc.) viram
método público no contrato ou acesso movido para dentro do renderer.

### Fase 5 — Limpeza
Remover fallbacks mortos, `hasattr`/`callable` residuais sobre renderer, e
documentar o contrato no docstring da base.

## Validação
Suíte completa após cada fase; fases 3–4 também com smoke manual do chat
(`python -m quimera`) verificando banner, mensagens neutras, failover e feed.

## Status
- [x] Fase 0 — inventário em tests/test_renderer_base.py (CONTRACT)
- [x] Fase 1 — quimera/ui/base.py; formatters movidos para ui/messages.py
- [x] Fase 2 — 21 fakes herdam a base (exceto FakeRenderer com __getattr__
      catch-all em test_app_core_characterization.py, que já cobre o contrato);
      flush_quick da base delega a flush (semântica do fallback histórico)
- [x] Fase 3 — sniffing de método eliminado em app/ (restam internals e
      clear_screen, escopo da fase 4). notify_retry/failover agora decidem
      pelo flag declarado `supports_structured_agent_activity` (True no
      Textual), preservando o caminho prompt-aware dos renderers textuais.
- [x] Fase 4 — consumidores fora de app/ migrados para o contrato:
      AgentClient, InputBroker, Approval, SpyOutputPresenter e TaskProtocol;
      vazamentos de `_audit_logger`, `_agent_window_controller`, `_console` e
      `_window_manager` substituídos por capacidades públicas ou confinados ao
      renderer/compositor terminal.
- [x] Fase 5 — contrato documentado/expandido em RendererBase e IRenderer;
      fallbacks de método removidos dos consumidores principais. Permanece só o
      fallback de renderer callable em DisplayService para adaptadores legados
      de teste, fora do capability-sniffing do renderer.
