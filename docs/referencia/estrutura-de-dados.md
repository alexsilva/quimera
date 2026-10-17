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
| `mcp-connections.json` | Catálogo global de servidores MCP externos. |
| `mcp-server.json` | Padrões do servidor MCP HTTP. |
| `state/mcp-server.json` | Estado OAuth do servidor MCP HTTP (clients e tokens). |
| `workspaces/` | Dados isolados por projeto. |

## Workspace persistente

```text
workspaces/<cwd_hash>/
  workspace.json
  index.json
  mcp-connections.json
  mcp-server.json
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

`cwd_hash` é derivado do caminho absoluto do diretório de trabalho. O
`mcp-connections.json` combina exceções ao padrão global de ativação
(`mcp_active`, `mcp_inactive`; este também desliga conexões locais) com
definições locais; o `mcp-server.json`
sobrepõe os padrões globais chave a chave e lista em `http_clients_active` e
`http_clients_inactive` as exceções de clients OAuth liberados ou bloqueados
nesse workspace. Os formatos estão em
[MCP e ferramentas](../guia/mcp-e-ferramentas.md#arquivos-persistentes).

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
