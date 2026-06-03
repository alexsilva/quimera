# Solução de problemas

## O agente selecionado não roda

Verifique se a CLI está instalada e no `PATH`:

```bash
which claude
which codex
which gemini
which opencode
```

Também confira conexões persistidas:

```bash
quimera --list-connections
```

Se um override estiver errado, remova com `/disconnect <agente>` ou edite a conexão com `--connect`.

## MCP não aparece no agente

1. Confirme que você não iniciou com `--no-mcp`.
2. Rode com `--debug` para ver logs do servidor MCP.
3. Para HTTP, confira `/health` no host/porta configurados.
4. Para socket Unix, verifique se o plugin selecionado sabe injetar MCP.
5. Se usar token fixo, exporte a variável antes de iniciar:

   ```bash
   export QUIMERA_MCP_TOKEN='token-local'
   quimera --mcp-http --mcp-token-env QUIMERA_MCP_TOKEN
   ```

## Tool de shell foi bloqueada

O runtime aplica allowlist e denylist. Comandos perigosos ou fora da política são recusados. Prefira comandos pequenos e objetivos (`python`, `pytest`, `git`, `sed`, `find`, `head`, `tail`) e evite operações destrutivas.

## Mutação pendente de aprovação

Use:

```text
/approve
```

para liberar apenas a próxima mutação, ou:

```text
/approve-all
```

para liberar todas as próximas mutações da sessão. Revise o risco antes de usar autoaprovação.

## Contexto errado ou antigo

- Use `/context` para ver o contexto atual.
- Use `/context-edit` para corrigir.
- Use `/context-branch <nome>` para separar iniciativas.
- Use `/reset-state` para limpar estado operacional sem apagar histórico.

## Tasks presas

1. Liste tasks via agente/tool ou inspecione `tasks.db`.
2. Confira agentes ativos com `/agents`.
3. Use `/reload` se adicionou/removou conexão.
4. Verifique se há outro agente elegível para failover/review.

## UI ou prompt desorganizado

Rode com `--debug` e colete logs em `data/logs/render/`. Testes de baseline visual existem em `tests/fixtures/ui_baseline/` e ajudam a comparar regressões.
