# Configuração e operação

## Arquivos globais

O Quimera procura um diretório gravável entre locais candidatos do usuário e grava configurações globais nele.

| Arquivo | Conteúdo |
|---|---|
| `config.json` | Nome do usuário, janela de histórico, tema, densidade, idle timeout, visibilidade, threads, seleção de agentes e roteamento congelado/orquestrador. |
| `connections.json` | Overrides e agentes dinâmicos criados por `--connect`. |
| `.env` | Chaves simples `KEY=VALUE` para variáveis de modelo/API. |

As configurações MCP ficam no arquivo específico do workspace. Servidores e
variáveis de ambiente são persistidos juntos. Atualizações de configuração e
conexões usam substituição atômica e locks entre processos; arquivos `.lock`
adjacentes coordenam essas gravações e podem permanecer no diretório.

Se um JSON estiver inválido, ilegível ou não contiver um objeto, a leitura usa
defaults e registra um aviso. Novas alterações recusam sobrescrever o arquivo:
corrija seu conteúdo antes de salvar novamente. Uma falha de gravação preserva
o arquivo anterior.

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
| `QUIMERA_MCP_TOKEN` | Token opcional do entrypoint MCP socket standalone; não autentica o MCP HTTP. |
| `QUIMERA_MAX_STDERR_LINES` | Ajusta limite de linhas de stderr exibidas em resumo. |
| `SHELL` | Shell usado por ferramentas de comando quando não especificado. |

## Visibilidade de execução

`--visibility` controla como stdout/stderr de agentes aparecem:

- `quiet`: saída mais silenciosa e stderr truncado;
- `summary`: início/fim e resumo operacional;
- `full`: stdout e stderr completos.

Sem a flag, vale o valor salvo em `config.json` (ajustável pela janela
`/config`); a flag é um override apenas da sessão.

## Paralelismo e timeouts

- `--threads N` limita quantos agentes rodam em paralelo por rodada; sem a
  flag, vale o valor salvo em `config.json`.
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

Para ambiente com cliente MCP HTTP externo:

```bash
quimera --mcp-http
```
