# Testes e qualidade

## Rodar testes

```bash
pytest
```

Para um arquivo específico:

```bash
pytest tests/test_task_router.py
```

Para documentação:

```bash
mkdocs build --strict
```

## Áreas cobertas por testes

A suíte inclui testes para:

- protocolo e servidor MCP;
- ferramentas de runtime: arquivos, shell, patch, tasks e web;
- parser, drivers e integração OpenAI-compatible;
- roteamento, execução, review e failover de tasks;
- prompt, modos, contexto, estado compartilhado e TTL;
- app interativa, handlers, dispatch e turn management;
- UI, temas, wrapping e fixtures de baseline visual;
- configuração, plugins, conexões e workspace;
- evidências, storage, métricas e bugs.

## Fixtures visuais

`tests/fixtures/ui_baseline/` contém snapshots textuais por tema/densidade/largura. Ao alterar renderização, rode testes de UI e revise diffs com cuidado.

## Boas práticas de contribuição

1. Leia `AGENTS.md` antes de editar.
2. Faça mudanças pequenas e localizadas.
3. Prefira serviços existentes a adicionar lógica dentro de `QuimeraApp`.
4. Atualize docs quando alterar comportamento de CLI, comandos, tools, plugins ou persistência.
5. Rode testes relacionados e, quando possível, a suíte completa.
6. Para mudanças perceptíveis na UI, capture evidência visual ou atualize fixtures conscientemente.

## Comandos úteis de inspeção

```bash
rg "CMD_" quimera
rg "class AgentPlugin" quimera/plugins/base.py
rg "def score_plugin_for_task" quimera/runtime/task_planning.py
python - <<'PY'
from quimera.runtime.drivers.tool_schemas import resolve_tool_schemas
print([s['function']['name'] for s in resolve_tool_schemas()])
PY
```
