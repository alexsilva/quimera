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
| `grep_search` | Busca padrão de texto em arquivos. |
| `inspect_symbols` | Lista classes, funções e métodos de um arquivo Python via AST, sem executar código. |
| `write_file` | Cria ou sobrescreve arquivo; para mudanças parciais, prefira patch. |
| `apply_patch` | Aplica patch textual estruturado no workspace. |
| `replace_text` | Substitui texto literal em um arquivo dentro do workspace. |
| `remove_file` | Remove arquivo com confirmação por `dry_run=False`. |
| `run_shell` | Executa comando shell simples no workspace. |
| `exec_command` | Executa comando com sessão persistente e polling incremental. |
| `write_stdin` | Escreve no stdin de sessão aberta por `exec_command`. |
| `poll_command_session` | Consulta stdout/stderr incremental de sessão aberta, sem escrever no stdin. |
| `close_command_session` | Fecha sessão persistente de comando. |
| `git_status`, `git_diff`, `git_log`, `git_add`, `git_commit`, `git_branch`, `git_checkout`, `git_fetch`, `git_push` | Operações git estruturadas no repositório do workspace. |
| `browser_start`, `browser_navigate`, `browser_click`, `browser_type`, `browser_press`, `browser_mouse`, `browser_wait`, `browser_snapshot`, `browser_screenshot`, `browser_console`, `browser_network`, `browser_evaluate`, `browser_status`, `browser_close` | Automação de navegador (Chrome/Chromium via Playwright, extra `browser`); screenshots são salvos por sessão no diretório de artefatos do workspace. |
| `list_tasks` | Lista tasks com filtros. |
| `list_jobs` | Lista jobs. |
| `get_job` | Obtém detalhes de job. |
| `list_agents` | Lista os agentes ativos na sessão atual. |
| `web_search` | Pesquisa web via DuckDuckGo Lite. |
| `web_fetch` | Busca URL e extrai texto. |
| `todo_write` | Cria/atualiza TODOs da sessão. |
| `todo_list` | Lista TODOs da sessão. |
| `memory_save` | Salva/atualiza entrada estruturada da memória do workspace. |
| `memory_retrieve` | Recupera memória do workspace por namespace, key, prefixo ou tags. |
| `update_shared_state` | Atualiza o shared state da sessão. |
| `ask_user` | Faz uma pergunta ao usuário humano e aguarda resposta. |
| `delegate` | Delega tarefa para outro agente do pool Quimera. |

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

## Cross-MCP e `delegate`

A ferramenta `delegate` permite que um agente delegue uma tarefa a outro agente do pool. Ela é útil para dividir trabalho por especialidade: arquitetura para Gemini/Claude, edição para Codex/OpenCode, revisão para agentes fortes em review. O resultado entra no fluxo da sessão e pode ser usado como evidência ou contexto para a resposta final.

Cada delegação executa em um `AgentClient` isolado criado por chamada (dispatch de background), com cancel_event próprio: o agente delegado nunca interfere na execução ativa do agente que delegou, e delegações concorrentes não corrompem estado uma da outra. Cancelar o fluxo principal (ESC/Ctrl+C) também cancela as delegações em andamento. O client isolado herda o comportamento de pausa de idle timeout durante tools longas e o supervisor de processos da sessão, garantindo que subprocessos delegados sejam encerrados no shutdown.

## ChatGPT Secure MCP Tunnel via HTTP

O Quimera expõe dois endpoints de discovery padrão para compatibilidade com clientes OAuth-aware como o `tunnel-client` da OpenAI.

### Endpoints de discovery

| Endpoint | RFC | Descrição |
|---|---|---|
| `GET /.well-known/oauth-protected-resource/mcp` | RFC 9728 | Declara que `/mcp` é um recurso protegido. |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 | Metadados mínimos do servidor (apenas `issuer` e `scopes_supported`). |

> **Atenção:** esses endpoints existem apenas para discovery/compatibilidade. O Quimera **não implementa** fluxo OAuth completo — não há `authorization_endpoint`, `token_endpoint` nem code-flow. A autenticação real é sempre via Bearer token pré-configurado.

Esses endpoints são **públicos** (não exigem `Authorization`) e não expõem tokens, segredos ou dados internos.

### Configuração do servidor HTTP

Inicie o Quimera com HTTP MCP habilitado e o conjunto de ferramentas `agent`:

```bash
quimera --mcp-http --mcp-port 9095 --mcp-http-allow-tools agent
```

O conjunto `agent` publica apenas:
- `list_files`, `read_file`, `grep_search`, `inspect_symbols`, `list_tasks`, `list_jobs`, `get_job`, `memory_retrieve`, `todo_list` (somente leitura local)
- `git_status`, `git_log`, `git_diff`, `git_branch`, `git_fetch` (git somente leitura)
- `web_search`, `web_fetch` (leitura de rede)
- `delegate`, `list_agents` (delegação para agentes do pool)
- `replace_text`, `memory_save` e git de mutação (`git_add`, `git_commit`, `git_checkout`, `git_push`), sujeitos a aprovação

Ferramentas de escrita ampla e shell (`run_shell`, `write_file`, `apply_patch`, `remove_file`, `exec_command`) **não são expostas** por esse conjunto.

### Validação

```bash
# Checar endpoints de discovery
curl -i http://127.0.0.1:9095/.well-known/oauth-protected-resource/mcp
curl -i http://127.0.0.1:9095/.well-known/oauth-authorization-server

# Verificar saúde do servidor
curl http://127.0.0.1:9095/health
```

### Autenticação

O Quimera usa tokens Bearer pré-configurados. Configure antes de iniciar o servidor:

- Defina `QUIMERA_MCP_TOKEN` (ou use `--mcp-token-env`) com um token forte.
- Inclua `Authorization: Bearer <token>` em todas as requisições MCP.
- O header alternativo `X-Quimera-MCP-Token: <token>` também é aceito.
