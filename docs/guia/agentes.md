# Agentes e conexões

## Modelo de plugin

Cada agente é descrito por um `AgentPlugin` com:

- nome canônico (`name`) e prefixo slash (`prefix`);
- comando CLI ou driver OpenAI-compatible;
- estilo visual, ícone e aliases;
- capacidades (`capabilities`), tipos preferidos de task e tipos a evitar;
- flags como `supports_tools`, `supports_code_editing`, `supports_long_context`, `supports_task_execution` e `supports_warm_pool`;
- metadados de confiabilidade de ferramenta e tier base.

Esses metadados alimentam tanto a UI quanto o roteamento de tasks.

## Plugins nativos

| Agente | Prefixo | Driver padrão | Uso recomendado |
|---|---|---|---|
| Claude | `/claude` | CLI `claude` | Arquitetura, revisão, documentação, desenvolvimento geral e longo contexto. |
| Codex | `/codex` | CLI `codex exec` | Edição de código, testes, bug investigation e execução operacional. |
| Gemini | `/gemini` | CLI `gemini` | Arquitetura, revisão, documentação e contexto amplo. |
| OpenCode | `/opencode` | CLI `opencode` | Edições menores e revisão com output JSON. |
| Ollama Granite | `/ollama-granite4` | OpenAI-compatible | Backend local em `http://localhost:11434/v1`. |

O arquivo `AGENTS.md` do repositório descreve a taxonomia operacional por tiers e especialidades.

## Escolher agentes da sessão

```bash
quimera --agents claude codex gemini
```

Padrões com `*` são expandidos contra plugins disponíveis:

```bash
quimera --agents opencode*
```

## Conexões persistidas

O comando `--connect` cria ou edita uma conexão persistida no diretório de dados do Quimera. Ele pode ser interativo ou receber flags.

### Conexão CLI

```bash
quimera --connect meu-cli --driver cli --cmd minha-cli --flag valor
```

A conexão salva comando, cwd/env opcionais, formato de output e se o prompt deve ser passado como argumento.

### Conexão OpenAI-compatible

```bash
quimera --connect meu-api \
  --driver openai \
  --model modelo \
  --base-url https://api.exemplo/v1 \
  --api-key-env MINHA_API_KEY
```

`--extra-body` aceita JSON para parâmetros específicos de provedor:

```bash
quimera --connect deepseek --driver openai --model deepseek-reasoner \
  --base-url https://api.deepseek.com/v1 --api-key-env DEEPSEEK_API_KEY \
  --extra-body '{"thinking":{"type":"enabled"}}'
```

## Herdar comando de plugin base

Alguns plugins CLI têm placeholder `--model=`. É possível criar uma conexão usando `--base` e `--model`:

```bash
quimera --connect opencode-qwen --base opencode --model qwen/qwen3-coder
```

## Listar e remover conexões

```bash
quimera --list-connections
```

No chat, use:

```text
/disconnect meu-agente
/reload
```

`/reload` reaplica conexões persistidas e atualiza a lista de plugins conhecidos.

## Integração MCP por agente

- Claude recebe `--mcp-config` JSON.
- Codex recebe argumentos `-c mcp_servers.quimera.*`.
- OpenCode recebe `OPENCODE_CONFIG_CONTENT`.
- Agentes OpenAI-compatible usam tools nativas quando suportadas pelo driver.
- Plugins sem integração MCP continuam podendo rodar como CLI normal, mas não recebem o runtime via MCP.
