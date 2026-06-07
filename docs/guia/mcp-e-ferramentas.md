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

## ChatGPT Secure MCP Tunnel via HTTP

O Quimera expõe metadados OAuth padrão para compatibilidade com o `tunnel-client` da OpenAI.

### Endpoints de descoberta

| Endpoint | RFC | Descrição |
|---|---|---|
| `GET /.well-known/oauth-protected-resource/mcp` | RFC 9728 | Identifica o servidor de autorização responsável pelo recurso `/mcp`. |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 | Descreve endpoints de autorização e token. |

Esses endpoints são **públicos** (não exigem `Authorization`) e não expõem tokens, segredos ou dados internos do Quimera.

### Configuração do perfil HTTP para o tunnel

Gere o perfil `quimera-local.yaml` usando o `tunnel-client init --mcp-server-url http://127.0.0.1:9095/mcp`. O Quimera deve ser iniciado com o perfil `agent` para expor `call_agent`:

```bash
quimera --mcp-http --mcp-http-port 9095 --mcp-http-profile agent
```

O perfil `agent` publica apenas:
- `list_files`, `read_file`, `grep_search`, `list_tasks`, `list_jobs`, `get_job`, `todo_list` (somente leitura local)
- `web_search`, `web_fetch` (leitura de rede)
- `call_agent` (delegação para agentes do pool)

Ferramentas de escrita e shell (`run_shell`, `write_file`, `apply_patch`, `remove_file`, `exec_command`) **não são expostas** por esse perfil.

### Validação

```bash
# Checar metadados OAuth
curl -i http://127.0.0.1:9095/.well-known/oauth-protected-resource/mcp
curl -i http://127.0.0.1:9095/.well-known/oauth-authorization-server

# Verificar saúde do servidor
curl http://127.0.0.1:9095/health

# Diagnóstico do tunnel-client
tunnel-client doctor --profile quimera-local --explain
```

### Autenticação

O Quimera usa tokens Bearer pré-configurados, não um fluxo OAuth completo. Para clientes que passam pelo tunnel:

- Configure `QUIMERA_MCP_TOKEN` (ou `--mcp-token-env`) com um token forte.
- Inclua o header `Authorization: Bearer <token>` em todas as requisições MCP.
- O header alternativo `X-Quimera-MCP-Token: <token>` também é aceito.
