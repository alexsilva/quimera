# Estrutura de dados

## Diretório global

O Quimera escolhe um diretório gravável de dados do usuário. Em instalações Linux comuns, o caminho efetivo é:

```text
~/.local/share/quimera/
```

Conteúdo comum:

| Caminho | Descrição |
|---|---|
| `config.json` | Preferências globais. |
| `connections.json` | Agentes dinâmicos e overrides de conexão. |
| `.env` | Variáveis simples de ambiente. |
| `workspaces/` | Dados isolados por projeto. |

## Workspace persistente

```text
workspaces/<cwd_hash>/
  workspace.json
  index.json
  data/
    context/
      _default/persistent.md
      session.md
      previous_session.md
    prompts/
      _default/prompt.md
    logs/
      sessions/
      render/
      metrics/
    tasks.db
```

`cwd_hash` é derivado do caminho absoluto do diretório de trabalho.

## Workspace temporário

```text
/tmp/quimera/<cwd_hash>/
  data/logs/render/
  data/logs/metrics/
  mcp-*.sock
```

Use esse local para artefatos efêmeros de sessão, não para dados duráveis.

## Banco de tasks

`tasks.db` é SQLite e armazena jobs e tasks. Os registros de task carregam, entre outros campos:

- `id`, `job_id`, `description`, `body`;
- `status`, `task_type`, `origin`, `assigned_to`;
- `result`, `notes`, `priority`;
- timestamps e autoria;
- campos operacionais de review, tentativas e agentes falhos.

## Estado compartilhado

`shared_state` não é um arquivo único obrigatório: ele é mantido pela sessão, pode ser serializado em snapshots e é filtrado antes de entrar no prompt. Chaves de agentes expiram por TTL de turnos se não forem reafirmadas.
