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
- `quimera/runtime/tools/delegate.py`: `delegate` — delegação entre agentes via MCP (cross-MCP).
- `quimera/profiles/`: catálogo de agentes e metadados de capacidade. Cada profile injeta configuração MCP no formato nativo do agente.
- `quimera/ui/`: renderização terminal (temas, densidade, stream e resumo).
- `quimera/prompt.md` / `quimera/task_prompt.md`: templates de prompt com blocos condicionais `<!-- IF:mcp_enabled -->`.

## Requisitos

- Python `>=3.10`.
- Dependência base: `rich`.
- CLIs/API dos agentes que você pretende usar.

Exemplos comuns:
- `claude` CLI
- `codex` CLI
- backend OpenAI-compatible local/remoto (ex.: Ollama)

## Instalação

```bash
git clone git@github.com:alexsilva/quimera.git
cd quimera
pip install -e .
```

Opcional para documentação local:

```bash
pip install -e ".[docs]"
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
- `--driver-repl <profile>`: REPL para testar driver `openai_compat`.
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
- `/reset [state|history|all]`: limpa `shared_state`, histórico ou ambos.
- `/clear`, `/help`, `/exit`.


## Testes interativos locais

O Quimera inclui agentes fake para validar o app sem provedores externos. Eles não entram no uso normal: use `--test` para ativar esse conjunto e deixar somente os fake na rodada:

- `fake-cli`: agente CLI local determinístico (`python -m quimera.devtools.fake_agents cli`).
- `fake-openai`: profile OpenAI-compatible apontando para um servidor local fake com tool calling nativo.
- `fake-cli-delegation`: agente CLI que usa MCP `delegate` para delegar ao `fake-openai`.
- `fake-openai-mcp-cli`: agente CLI que chama o backend OpenAI-compatible fake diretamente e executa tool calls via MCP do Quimera.

Exemplo rápido:

```bash
python quimera.py --test --agents fake-cli-delegation fake-openai --visibility full
```

Com `--test`, o app registra os fake profiles, inicia automaticamente o backend OpenAI-compatible fake em uma porta livre e aplica overrides somente no processo. O comando `python -m quimera.devtools.fake_agents openai-server` continua disponível apenas para debug manual.

Veja o guia completo em [docs/desenvolvimento/testes.md](./docs/desenvolvimento/testes.md#testador-interativo-local-com-agentes-fake).

## Agentes e profiles

O projeto registra profiles para:

- `claude`
- `codex`
- `chatgpt` (driver `openai_compat`)
- família `ollama-*` (driver `openai_compat`)
- família `opencode*`

Também é possível registrar agentes dinâmicos via `--connect` ou `/connect`.

Detalhes de capacidades por tier/especialidade: [AGENTS.md](./AGENTS.md).


## Sistema MCP (Model Context Protocol)

O Quimera implementa o protocolo MCP (`2025-11-25`, com compatibilidade de negociação para versões anteriores) via JSON-RPC 2.0, expondo ferramentas, resources, prompts e completion do runtime para agentes compatíveis. Por padrão, um servidor MCP é iniciado por sessão em um socket Unix com autenticação por token (fixo via env ou aleatório por sessão).

O servidor anuncia as capabilities MCP mais recentes usadas pelo Quimera:

- `tools` com `tools/list`, `tools/call` e `notifications/tools/list_changed`;
- `resources` com `resources/list`, `resources/read`, `resources/templates/list`, `resources/subscribe` e `resources/unsubscribe`;
- `prompts` com `prompts/list` e `prompts/get`;
- `completions` com `completion/complete`;
- `logging` com `logging/setLevel`.

O MCP interno via socket Unix é core do Quimera: ele inicia por padrão, é entregue a Claude, Codex, OpenCode e agentes locais similares, e expõe todas as ferramentas registradas no `ToolExecutor`. Ferramentas sensíveis continuam protegidas por `ToolPolicy`, permissões de path e fluxo de approval no executor.

O MCP HTTP é uma extensão externa opcional para clientes remotos. Ele usa uma instância separada de `MCPServer`, com token externo, CORS configurável e allowlist aplicada apenas ao HTTP. A allowlist HTTP nunca reduz as ferramentas disponíveis para o socket interno.

Ferramentas são executadas por chamadas estruturadas: MCP para agentes CLI/MCP-capazes e, quando o backend OpenAI-compatible oferece suporte nativo a tool calling, chamadas nativas do driver. Em ambos os caminhos, a execução concreta converge para `ToolExecutor.execute(ToolCall(...))`, com `ToolPolicy`, approval e `ToolRegistry` centralizando validação, permissão, auditoria e despacho para os handlers do runtime.

A ferramenta `delegate` permite que qualquer agente MCP-capaz delegue trabalho a outro agente no pool da sessão (disponível apenas quando MCP está ativo).

### Perfis embutidos

| Perfil | Tools expostas | Uso recomendado |
|---|---|---|
| `read-local` | `list_files`, `read_file`, `grep_search`, `list_tasks`, `list_jobs`, `get_job`, `todo_list` | Exposição externa mais restrita, sem ferramentas com acesso à rede. |
| `read` | Tudo de `read-local` + `web_search`, `web_fetch` | Leitura com pesquisa/fetch web quando a rede é necessária. |
| `agent` | Tudo de `read` + `delegate` | Delegação entre agentes sem liberar `run_shell`, `exec_command`, `write_file`, `remove_file` ou `apply_patch`. |
| `all` | Sem filtro de allowlist | Apenas desenvolvimento local ou rede privada muito confiável. |

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

## Ciclo de vida de task

Estado típico:

`pending -> in_progress -> pending_review -> completed`

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
- **cross-MCP**: `delegate` — delega tarefas a outro agente no pool (disponível apenas quando MCP está ativo)

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
