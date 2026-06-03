# Configuração e operação

## Arquivos globais

O Quimera procura um diretório gravável entre locais candidatos do usuário e grava configurações globais nele.

| Arquivo | Conteúdo |
|---|---|
| `config.json` | Nome do usuário, janela de histórico, tema, densidade e idle timeout. |
| `connections.json` | Overrides e agentes dinâmicos criados por `--connect`. |
| `.env` | Chaves simples `KEY=VALUE` para variáveis de modelo/API. |

## Configurações persistíveis pela CLI

```bash
quimera --set-theme panel
quimera --set-history-window 20
```

Temas disponíveis na CLI incluem `panel`, `chat`, `rule`, `minimal`, `card` e `line`.

## Variáveis úteis

| Variável | Uso |
|---|---|
| `OPENAI_API_KEY` | Chave padrão para conexões OpenAI-compatible. |
| `QUIMERA_MCP_TOKEN` | Token fixo para clientes MCP externos quando usado com `--mcp-token-env`. |
| `QUIMERA_MAX_STDERR_LINES` | Ajusta limite de linhas de stderr exibidas em resumo. |
| `SHELL` | Shell usado por ferramentas de comando quando não especificado. |

## Visibilidade de execução

`--visibility` controla como stdout/stderr de agentes aparecem:

- `quiet`: saída mais silenciosa e stderr truncado;
- `summary`: início/fim e resumo operacional;
- `full`: stdout e stderr completos.

## Paralelismo e timeouts

- `--threads N` limita quantos agentes rodam em paralelo por rodada.
- `--timeout N` define timeout de execução de agentes.
- `--idle-timeout N` define timeout de inatividade do input.

## Diretório de trabalho

A sessão usa o diretório atual como workspace. Para REPL de driver, `--working-dir DIR` muda o cwd usado no teste do driver.

## Operação recomendada

Para trabalho diário:

```bash
quimera --agents claude codex --threads 2 --visibility summary
```

Para investigação detalhada:

```bash
quimera --agents claude codex gemini --visibility full --debug
```

Para ambiente com cliente MCP externo:

```bash
export QUIMERA_MCP_TOKEN='token-local-forte'
quimera --mcp-http --mcp-token-env QUIMERA_MCP_TOKEN
```
