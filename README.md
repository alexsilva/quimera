# Quimera

Orquestrador multiagente para engenharia de software no terminal.

O Quimera coordena agentes (CLI e OpenAI-compatible), mantém estado compartilhado por workspace, roteia tarefas com balanceamento de carga e executa tools com política de segurança.

## Objetivo

- Conversar com agentes especializados no mesmo fluxo de trabalho.
- Criar tarefas explícitas via `/task` e processá-las em background com review cruzado.
- Persistir contexto operacional entre sessões sem depender de histórico infinito.

## Arquitetura em alto nível

- `quimera/cli.py`: entrada principal da aplicação e flags de execução.
- `quimera/app/`: loop interativo, protocolo de handoff, comandos slash e orquestração de rodada.
- `quimera/runtime/`: drivers, parser de tool calls, políticas e execução de ferramentas.
- `quimera/runtime/mcp_server.py`: servidor MCP (Model Context Protocol) — expõe tools do runtime via JSON-RPC 2.0 sobre stdio/socket Unix.
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
- `--mcp` / `--no-mcp`: ativa/desativa servidor MCP (padrão: ativado).
- `--mcp-socket <path>`: caminho personalizado para o socket Unix do MCP.

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

O Quimera implementa o protocolo MCP (`2024-11-05`) via JSON-RPC 2.0, expondo as ferramentas do runtime para agentes compatíveis. Um servidor MCP é iniciado por sessão em um socket Unix com autenticação por token aleatório.

### Arquitetura MCP

```
Agente (Codex/Claude/OpenCode)
  │  JSON-RPC 2.0 via stdio
  ▼
Proxy stdio→socket (plugin do agente)
  │  conexão Unix socket com token de autenticação
  ▼
MCPServer (quimera/runtime/mcp_server.py)
  │  tools/list → resolve_tool_schemas()
  │  tools/call → ToolExecutor.execute()
  ▼
ToolRegistry → handlers (read_file, run_shell, call_agent, ...)
```

### Habilitação

Por padrão o MCP é ativado automaticamente. Controles via CLI:

- `--mcp` (padrão): ativa servidor MCP.
- `--no-mcp`: desativa completamente.
- `--mcp-socket <path>`: caminho personalizado para o socket Unix.

### Injeção por plugin

Cada agente recebe a configuração MCP no formato que seu CLI/API entende:

| Agente | Formato de injeção | Mecanismo |
|---|---|---|
| **Claude** | `--mcp-config` JSON (`mcpServers.quimera`) | Argumento CLI |
| **Codex** | `-c mcp_servers.quimera.*` (TOML-style) | Argumento CLI |
| **OpenCode** | `OPENCODE_CONFIG_CONTENT` env var | Variável de ambiente |
| **Gemini** | Sem suporte a MCP | — |

### Ferramentas expostas via MCP

As 15 ferramentas do runtime (`TOOL_SCHEMAS` em `runtime/drivers/tool_schemas.py`) são filtradas dinamicamente por registro, política e disponibilidade:

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
    "task": "Implementar função de parser",
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
- **Token de autenticação**: cada sessão gera um `secrets.token_urlsafe(32)` único.

### MCP-First Mode

Quando o MCP está ativo, o parser de protocolo (`app/protocol.py`) ignora envelopes textuais legados (`<handoff>...</handoff>`) — agentes usam exclusivamente a tool `call_agent` via MCP, evitando dupla delegação.

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

Ferramentas suportadas pelo runtime (expostas via MCP e/ou parser textual):

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

- `QUIMERA_MCP_TOKEN`: token de autenticação para modo standalone.
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
pytest -q tests/test_runtime_mcp_server.py tests/test_agents.py
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
