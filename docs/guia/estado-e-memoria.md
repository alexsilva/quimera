# Estado, memória e evidências

## Workspace

Cada diretório de projeto recebe um hash estável. O Quimera usa esse hash para isolar dados persistentes em um diretório global de dados do usuário e dados temporários em `/tmp/quimera/<hash>`.

Dados persistentes típicos:

```text
~/.local/share/quimera/
  config.json
  connections.json
  .env
  workspaces/<cwd_hash>/
    workspace.json
    index.json
    data/
      context/
      prompts/
      logs/
      tasks.db
```

Dados temporários típicos:

```text
/tmp/quimera/<cwd_hash>/
  data/logs/render/
  data/logs/metrics/
  mcp-*.sock
```

## Contexto persistente

`/context-edit` edita o arquivo `persistent.md` do workspace. A branch ativa muda o caminho do contexto:

```text
data/context/_default/persistent.md
data/context/minha_branch/persistent.md
```

Use `/context-branch <nome>` para separar memórias de iniciativas diferentes no mesmo repositório.

## Contexto de sessão

A sessão atual mantém contexto efêmero em `session.md`. Ao final ou durante snapshots, o Quimera pode salvar histórico recente e carregar resumo de sessão anterior para warm-start.

## `shared_state`

`shared_state` é o estado operacional compartilhado entre runtime, prompt e agentes. Ele tem dois grupos de chaves:

### Chaves que agentes podem atualizar

- `goal`
- `goal_canonical`
- `decisions`
- `current_step`
- `acceptance_criteria`
- `allowed_scope`
- `non_goals`
- `out_of_scope_notes`
- `evidence`
- `next_step`

Essas chaves podem vir em blocos `[STATE_UPDATE]` e têm TTL por turno para evitar contexto velho.

### Chaves escritas pelo sistema

- `task_overview`
- `completed_task_results`
- `spy_last_turn_detail`
- `working_dir`
- `workspace_root`
- `agent_todos`

O prompt principal não expõe tudo: o payload é filtrado para reduzir ruído e evitar vazamento de estado operacional irrelevante.

## Evidências

O pacote `quimera/evidence` trata evidências como dados estruturados. O objetivo é transformar outputs de agentes, tool calls e resultados de execução em material reutilizável para revisão, prompts e auditoria.

Uma evidência deve responder:

- qual ação foi feita;
- onde foi feita;
- qual resultado foi observado;
- qual comando, arquivo, trecho ou evento comprova o resultado.

## Logs e métricas

Com `--debug`, o Quimera ativa auditoria de renderização em `data/logs/render/`. Métricas por sessão ficam em `data/logs/metrics/`. Esses arquivos ajudam a diagnosticar problemas de UI, latência, redisplay, turnos, delegation e execução.
