# MCP e ferramentas

## Papel do MCP

O Quimera inicia um servidor MCP embutido por sessão para expor ferramentas do runtime aos agentes compatíveis. O servidor usa JSON-RPC 2.0 e negocia versões do protocolo MCP, mantendo compatibilidade com versões anteriores.

Transportes suportados:

- **socket Unix**: padrão, com proxy stdio para CLIs;
- **HTTP Streamable**: ativado por `--mcp-http`, útil para clientes externos locais;
- **stdio standalone**: `python -m quimera.runtime.mcp` para usos isolados.

## Autenticação

Quando há token de sessão, clientes socket enviam uma primeira linha JSON com `quimera_auth_token`. No HTTP, o token pode ser enviado como Bearer token. A variável padrão para token fixo é `QUIMERA_MCP_TOKEN`, customizável com `--mcp-token-env`.

## Métodos MCP principais

O servidor implementa, entre outros:

- `initialize` e `notifications/initialized`;
- `tools/list`;
- `tools/call`;
- `ping`;
- recursos, prompts e completion conforme suporte do runtime.

Chamadas de tool podem ser executadas em thread pool, com cancelamento e progresso.

## Ferramentas disponíveis

| Tool | Função |
|---|---|
| `list_files` | Lista arquivos/diretórios dentro do workspace. |
| `read_file` | Lê arquivo com intervalo opcional de linhas. |
| `write_file` | Cria ou sobrescreve arquivo; para mudanças parciais, prefira patch. |
| `apply_patch` | Aplica patch textual estruturado no workspace. |
| `grep_search` | Busca padrão de texto em arquivos. |
| `remove_file` | Remove arquivo com confirmação por `dry_run=False`. |
| `run_shell` | Executa comando shell simples no workspace. |
| `exec_command` | Executa comando com sessão persistente e polling incremental. |
| `write_stdin` | Escreve ou faz polling em sessão aberta por `exec_command`. |
| `close_command_session` | Fecha sessão persistente de comando. |
| `list_tasks` | Lista tasks com filtros. |
| `list_jobs` | Lista jobs. |
| `get_job` | Obtém detalhes de job. |
| `web_search` | Pesquisa web via DuckDuckGo Lite. |
| `web_fetch` | Busca URL e extrai texto. |
| `todo_write` | Cria/atualiza TODOs da sessão. |
| `todo_list` | Lista TODOs da sessão. |
| `call_agent` | Delega tarefa para outro agente do pool Quimera. |

## Política de segurança

O runtime usa `ToolRuntimeConfig` para definir:

- raiz do workspace e raízes de leitura permitidas;
- timeout de comandos;
- limite de output, leitura de arquivo e resultados de busca;
- exigência de aprovação para mutações;
- allowlist de comandos shell comuns;
- denylist para padrões perigosos como `rm -rf`, `sudo`, `shutdown`, `mkfs`, `dd` e permissões recursivas arriscadas.

## Aprovação

Ferramentas de mutação podem exigir aprovação. Na app interativa, `/approve` libera a próxima mutação e `/approve-all` muda o comportamento para autoaprovação. Em execução não interativa ou MCP standalone, o handler de aprovação pode ser configurado pelo runtime.

## Cross-MCP e `call_agent`

A ferramenta `call_agent` permite que um agente delegue uma tarefa a outro agente do pool. Ela é útil para dividir trabalho por especialidade: arquitetura para Gemini/Claude, edição para Codex/OpenCode, revisão para agentes fortes em review. O resultado entra no fluxo da sessão e pode ser usado como evidência ou contexto para a resposta final.
