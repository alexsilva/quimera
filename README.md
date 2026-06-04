# Quimera

Orquestrador multiagente para engenharia de software no terminal.

O Quimera coordena agentes (CLI e OpenAI-compatible), mantém estado compartilhado por workspace, roteia tarefas com balanceamento de carga e executa ferramentas estruturadas com política de segurança.

## Objetivo

- Conversar com agentes especializados no mesmo fluxo de trabalho.
- Criar tarefas explícitas via `/task` e processá-las em background com review cruzado.
- Persistir contexto operacional entre sessões sem depender de histórico infinito.

## Arquitetura em alto nível

- `quimera/cli.py`: entrada principal da aplicação e flags de execução.
- `quimera/app/`: loop interativo, protocolo de respostas/estado, comandos slash e orquestração de rodada.
- `quimera/runtime/`: drivers, modelos `ToolCall`/`ToolResult`, registry, políticas e execução segura de ferramentas.
- `quimera/runtime/mcp/server.py`: servidor MCP (Model Context Protocol) — expõe tools do runtime via JSON-RPC 2.0 sobre stdio/socket Unix.
- `quimera/runtime/task_planning.py`: classificação de task e scoring de roteamento.
- `quimera/runtime/tasks.py`: persistência de jobs/tasks em SQLite.
- `quimera/runtime/tools/handoff.py`: `call_agent` — delegação entre agentes via MCP (cross-MCP).
- `quimera/plugins/`: catálogo de agentes e metadados de capacidade. Cada plugin injeta configuração MCP no formato nativo do agente.
- `quimera/ui/`: renderização terminal (temas, densidade, stream e resumo).
- `quimera/prompt.md` / `quimera/task_prompt.md`: templates de prompt com blocos condicionais `<!-- IF:mcp_enabled -->`.

## Requisitos

- Python `>=3.10`.
- Dependência base: `rich`.
- CLIs/API dos agentes que você pretende usar.

Exemplos comuns:
- `claude` CLI
- `codex` CLI
- `gemini` CLI
- backend OpenAI-compatible local/remoto (ex.: Ollama)

## Instalação

```bash
git clone git@github.com:alexsilva/quimera.git
cd quimera
pip install -e .
```

Opcional (drivers via API compatível e input interativo com histórico/completion):

```bash
pip install -e ".[api,ollama,interactive]"
```

## Execução

Se o script `quimera` estiver no `PATH`:

```bash
quimera
```

Alternativa equivalente:

```bash
python quimera.py
```

## CLI (flags)

Principais flags:

- `--agents <a1> <a2>`: define agentes ativos na sessão.
- `--threads N`: paralelismo de rodadas de chat.
- `--timeout N`: timeout de execução de agente (s).
- `--idle-timeout N`: timeout de inatividade de input (s).
- `--visibility quiet|summary|full`: nível de detalhe da execução.
- `--theme panel|chat|rule|minimal`: tema visual da sessão.
- `--set-theme <tema>`: persiste tema e encerra.
- `--connect <agente>`: cria/edita conexão persistida do agente.
- `--list-connections`: lista conexões persistidas.
- `--driver-repl <plugin>`: REPL para testar driver `openai_compat`.
- `--mcp-socket [path]` / `--mcp-http`: seleciona socket Unix ou HTTP; sem flags, usa socket Unix.
- `--no-mcp`: desativa o servidor MCP.
- `--mcp-http --mcp-host 127.0.0.1 --mcp-port 9090`: usa MCP HTTP embutido em vez do socket Unix.

Ajuda completa:

```bash
python quimera.py --help
```

## Comandos no chat

- `/task <descrição>`: cria task humana explícita e roteia para o melhor agente.
- `/planning <msg>`: modo leitura para planejamento.
- `/analysis <msg>`: modo leitura para análise.
- `/design <msg>`: modo design sem execução de código.
- `/review <msg>`: modo revisão sem edição.
- `/execute <msg>`: remove restrições de modo e libera execução.
- `/agents`: lista agentes ativos.
- `/connect <agente>`: configura conexão no próprio chat.
- `/prompt [agente]`: preview do prompt final (debug operacional).
- `/context`: mostra contexto persistente/sessão.
- `/context-edit`: edita contexto persistente no editor.
- `/edit`: abre editor para compor mensagem longa.
- `/file <caminho>`: envia conteúdo de arquivo como mensagem.
- `/approve`: pré-aprova a próxima tool mutation.
- `/approve-all`: aprova automaticamente mutações subsequentes.
- `/reset-state`: limpa `shared_state` sem apagar histórico.
- `/clear`, `/help`, `/exit`.

## Agentes e plugins

O projeto registra plugins para:

- `claude`
- `codex`
- `gemini`
- `chatgpt` (driver `openai_compat`)
- família `ollama-*` (driver `openai_compat`)
- família `opencode*`

Também é possível registrar agentes dinâmicos via `--connect` ou `/connect`.

Detalhes de capacidades por tier/especialidade: [AGENTS.md](./AGENTS.md).

## Sistema MCP (Model Context Protocol)

O Quimera implementa o protocolo MCP (`2025-11-25`, com compatibilidade de negociação para versões anteriores) via JSON-RPC 2.0, expondo ferramentas, resources, prompts e completion do runtime para agentes compatíveis. Por padrão, um servidor MCP é iniciado por sessão em um socket Unix com autenticação por token (fixo via env ou aleatório por sessão).

### Arquitetura MCP

```
Agente (Codex/Claude/OpenCode)
  │  JSON-RPC 2.0 via stdio
  ▼
Proxy stdio→socket (plugin do agente)
  │  conexão Unix socket com token de autenticação
  ▼
MCPServer (quimera/runtime/mcp/server.py)
  │  tools/list → resolve_tool_schemas()
  │  tools/call estruturado → ToolExecutor.execute()
  ▼
ToolPolicy/approval → ToolRegistry → handlers (read_file, run_shell, call_agent, ...)
```


Ferramentas são executadas por chamadas estruturadas: MCP para agentes CLI/MCP-capazes e, quando o backend OpenAI-compatible oferece suporte nativo a tool calling, chamadas nativas do driver. Em ambos os caminhos, a execução concreta converge para `ToolExecutor.execute(ToolCall(...))`, com `ToolPolicy`, approval e `ToolRegistry` centralizando validação, permissão, auditoria e despacho para os handlers do runtime.

### Habilitação

Por padrão o MCP é ativado automaticamente. Controles via CLI:

- `--mcp-socket [path]`: seleciona explicitamente o transporte socket Unix e opcionalmente define o path.
- padrão sem flags MCP: inicia o transporte socket Unix com path temporário por workspace.
- `--no-mcp`: desativa completamente.
- `--mcp-http`: seleciona o transporte Streamable HTTP em `/mcp`.
- `--mcp-port <porta>`: porta do servidor HTTP MCP (padrão: `9090`).
- `--mcp-host <host>`: host do servidor HTTP MCP (padrão: `127.0.0.1`).
- `--mcp-token-env <VAR>`: variável de ambiente usada como token MCP fixo para clientes externos (padrão: `QUIMERA_MCP_TOKEN`; se vazia, token aleatório por sessão).
- `--mcp-http-allow-tools <read|all|CSV>`: allowlist de tools expostas no MCP HTTP externo. O padrão `read` publica apenas tools de leitura (`list_files`, `read_file`, `grep_search`, `list_tasks`, `list_jobs`, `get_job`, `web_search`, `web_fetch`, `todo_list`); use `all` somente em redes confiáveis ou informe nomes separados por vírgula.

### Injeção por plugin

Cada agente recebe a configuração MCP no formato que seu CLI/API entende:

| Agente | Formato de injeção | Mecanismo |
|---|---|---|
| **Claude** | socket: `--mcp-config` stdio; HTTP: `type=http`, `url=http://.../mcp` | Argumento CLI |
| **Codex** | socket: `-c mcp_servers.quimera.command/args`; HTTP: `-c mcp_servers.quimera.url/transport/headers.*` | Argumento CLI |
| **OpenCode** | socket: `type=local`; HTTP: `type=remote`, `url`, `headers` | `OPENCODE_CONFIG_CONTENT` |
| **Gemini** | Sem suporte a MCP | — |

### Features expostas via MCP

O servidor anuncia as capabilities MCP mais recentes usadas pelo Quimera:

- `tools` com `tools/list`, `tools/call` e `notifications/tools/list_changed`;
- `resources` com `resources/list`, `resources/read`, `resources/templates/list`, `resources/subscribe` e `resources/unsubscribe`;
- `prompts` com `prompts/list` e `prompts/get`;
- `completions` com `completion/complete`;
- `logging` com `logging/setLevel`.

As ferramentas do runtime (`TOOL_SCHEMAS` em `runtime/drivers/tool_schemas.py`) são filtradas dinamicamente por registro, política e disponibilidade:

- `list_files`, `read_file`, `write_file`, `remove_file`, `apply_patch`
- `grep_search`, `run_shell`, `exec_command`, `write_stdin`, `close_command_session`
- `list_tasks`, `list_jobs`, `get_job`
- `web_search`, `web_fetch`
- `call_agent` — **cross-MCP**: delega tarefas a outro agente no pool

### Cross-MCP: call_agent

O mecanismo central de interoperabilidade entre agentes é a ferramenta `call_agent`, que permite que qualquer agente MCP-capaz delegue trabalho a outro agente no pool da sessão.

```json
{
  "name": "call_agent",
  "arguments": {
    "agent_name": "codex",
    "task": "Implementar função de leitura",
    "context": "contexto opcional",
    "fallback_agents": ["claude", "gemini"],
    "handoffs": [
      {"agent_name": "claude", "task": "Revisar o resultado"}
    ]
  }
}
```

Características:
- **Failover automático**: `fallback_agents` é tentado se o primário falhar.
- **Cadeias de delegação**: `handoffs` permite múltiplos passos em uma chamada.
- **Validação de agentes ativos**: o alvo é verificado contra o pool antes da execução.
- **Token de autenticação**: usa `QUIMERA_MCP_TOKEN`/`--mcp-token-env` quando configurado; caso contrário cada sessão gera um `secrets.token_urlsafe(32)` único.

### Delegação entre agentes via MCP

Delegação entre agentes acontece pela tool MCP `call_agent`. Agentes MCP-capazes chamam essa ferramenta para acionar outro agente do pool da sessão, com validação do alvo, contexto estruturado, fallback opcional e cadeias de delegação declaradas nos argumentos da tool.

### MCP HTTP embutido

Para iniciar o app com MCP Streamable HTTP embutido em vez do socket Unix padrão:

```bash
python quimera.py --mcp-http --mcp-host 127.0.0.1 --mcp-port 9090
```

Nesse modo, agentes compatíveis recebem `http://127.0.0.1:9090/mcp` na inicialização, com o mesmo token da sessão enviado como `Authorization: Bearer <token>` (ou `X-Quimera-MCP-Token` para clientes simples). Por segurança, o transporte HTTP externo publica somente tools de leitura por padrão; ajuste com `--mcp-http-allow-tools all` ou `--mcp-http-allow-tools read_file,grep_search` quando necessário. As respostas CORS expõem `MCP-Session-Id` via `Access-Control-Expose-Headers` para clientes web conseguirem reutilizar a sessão Streamable HTTP.

Para conectar um cliente remoto, defina um token conhecido no ambiente antes de iniciar o Quimera e envie esse mesmo valor no header HTTP:

```bash
export QUIMERA_MCP_TOKEN=um-token-longo-e-aleatorio
python quimera.py --mcp-http --mcp-host 0.0.0.0 --mcp-port 9090

# Cliente remoto: initialize via POST /mcp. Use -i para ver MCP-Session-Id.
curl -i -X POST http://HOST:9090/mcp \
     -H "Authorization: Bearer um-token-longo-e-aleatorio" \
     -H "Content-Type: application/json" \
     -d '{
       "jsonrpc":"2.0",
       "id":1,
       "method":"initialize",
       "params":{
         "protocolVersion":"2025-11-25",
         "capabilities":{},
         "clientInfo":{"name":"curl","version":"manual"}
       }
     }'

# Próxima chamada: reutilize MCP-Session-Id e envie MCP-Protocol-Version.
curl -X POST http://HOST:9090/mcp \
     -H "Authorization: Bearer um-token-longo-e-aleatorio" \
     -H "Content-Type: application/json" \
     -H "MCP-Protocol-Version: 2025-11-25" \
     -H "MCP-Session-Id: <valor-retornado-no-initialize>" \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

Se `QUIMERA_MCP_TOKEN` (ou a variável apontada por `--mcp-token-env`) não estiver definida, o Quimera gera um token aleatório por sessão e apenas os plugins locais iniciados por ele recebem esse valor. O socket Unix continua sendo o padrão para preservar compatibilidade com o fluxo local existente.

Também funciona colocar o token no arquivo `.env` global do Quimera, carregado antes da inicialização do MCP:

```env
QUIMERA_MCP_TOKEN=um-token-longo-e-aleatorio
```

Esse `.env` fica em `~/.local/share/quimera/.env` (fallback: `/tmp/quimera/.env`), não no `.env` do diretório do projeto.

### Uso standalone

O servidor MCP pode ser executado independentemente:

```bash
python -m quimera.runtime.mcp
```

Conecta a um workspace via `QUIMERA_WORKSPACE` e expõe todas as ferramentas sem aprovação interativa (modo headless).

## Roteamento de tasks

Classificação automática de tipo:

- `code_edit`
- `architecture`
- `code_review`
- `bug_investigation`
- `test_execution`
- `documentation`
- `general`

Score base por agente considera:

- `base_tier`
- `preferred_task_types` e `avoid_task_types`
- capacidades (`supports_code_editing`, `supports_long_context`, `supports_tools`)
- confiabilidade de tools (`tool_use_reliability`) para `test_execution`/`bug_investigation`

Balanceamento de carga:

- `effective_score = base_score - open_tasks_do_agente`

Comportamento de resiliência:

- failover automático quando execução falha
- tracking de agentes que já falharam na task
- fallback para review por outro agente quando possível

## Ciclo de vida de task

Estado típico:

`pending -> in_progress -> pending_review -> completed`

Estados auxiliares:

- `failed`
- `proposed` / `approved` / `rejected` (fluxos legados)

Observações importantes:

- Apenas o humano cria task no chat (`/task`).
- `propose_task/approve_task` não são expostas para uso normal no chat.
- Resultado vazio, bloqueio explícito ou `[NEEDS_INPUT]` não conclui task.

## Ferramentas de runtime

Ferramentas suportadas pelo runtime (expostas via MCP e, quando disponível, pelo tool calling nativo do driver OpenAI-compatible):

- leitura/inspeção: `list_files`, `read_file`, `grep_search`
- edição: `apply_patch`, `write_file`, `remove_file`
- shell: `run_shell`, `exec_command`, `write_stdin`, `close_command_session`
- tasks/jobs: `list_tasks`, `list_jobs`, `get_job`
- web: `web_search`, `web_fetch`
- **cross-MCP**: `call_agent` — delega tarefas a outro agente no pool (disponível apenas quando MCP está ativo)

Política de segurança:

- allowlist de comandos shell (ex.: `git`, `pytest`, `python`, `ls`, `cat`, `sed`).
- denylist de padrões destrutivos.
- bloqueio de operadores de encadeamento (`;`, `&&`, `||`, `` ` ``, `$(`).
- caminhos restringidos ao workspace.
- mutações exigem aprovação por padrão.

## Persistência e diretórios

Base global:

- `~/.local/share/quimera` (fallback: `/tmp/quimera`)

Por workspace (hash do `cwd`):

- `workspaces/<hash>/workspace.json`
- `workspaces/<hash>/data/tasks.db`
- `workspaces/<hash>/data/context/persistent.md`
- `workspaces/<hash>/data/context/session.md`
- `workspaces/<hash>/data/logs/sessions/`
- `workspaces/<hash>/data/logs/metrics/`
- `workspaces/<hash>/state/metrics_state.json`
- `workspaces/<hash>/history`

Config global:

- `~/.local/share/quimera/config.json`
- `~/.local/share/quimera/connections.json`

Variáveis de ambiente MCP:

- `QUIMERA_MCP_TOKEN`: token de autenticação para modo standalone e para o MCP embutido quando usado com `--mcp-token-env` padrão.
- `QUIMERA_MCP_LOG_LEVEL`: nível de log do servidor MCP (padrão: `QUIMERA_LOG_LEVEL` ou `WARNING`).
- `QUIMERA_WORKSPACE`: diretório raiz do workspace para modo MCP standalone.

## Fluxo recomendado de uso

1. Inicie no diretório do projeto.
2. Ajuste agentes ativos com `--agents` ou `/connect`.
3. Use chat direto para ciclos curtos.
4. Abra `/task` para trabalho paralelo e auditável.
5. Use `/context` e `/prompt` para depuração de contexto/prompts.
6. Feche com `/exit` para persistir histórico e resumo.

## Testes

Execução rápida (núcleo):

```bash
pytest -q tests/test_public_api.py tests/test_runtime_task_planning.py tests/test_runtime_tools_tasks.py tests/test_ui.py
```

Testes MCP:

```bash
pytest -q tests/test_runtime_mcp_server.py tests/test_mcp_http_server.py tests/test_agents.py
```

Execução completa:

```bash
pytest -q
```

## Limitações conhecidas

- A qualidade final depende das CLIs/backends configurados para cada agente.
- Ambiente com poucas CLIs disponíveis reduz o ganho de roteamento multiagente.
- `pytest -q` completo pode incluir cenários dependentes de ambiente local.

## Status do projeto

Uso pessoal/experimental com foco em produtividade de engenharia no terminal.
