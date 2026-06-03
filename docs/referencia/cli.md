# Referência de CLI

A CLI é exposta pelo script `quimera` configurado em `pyproject.toml`.

## Uso geral

```bash
quimera [opções] [test_agent]
```

## Flags principais

| Flag | Descrição |
|---|---|
| `--name NOME [NOME ...]` | Define nome do usuário na sessão. |
| `--whoami` | Mostra identificação/configuração relacionada ao usuário. |
| `--debug` | Ativa métricas e auditoria de renderização em logs. |
| `--history-window N` | Define janela de histórico desta sessão. |
| `--visibility quiet|summary|full` | Controla detalhe da execução de agentes. |
| `--agents AGENTE [AGENTE ...]` | Lista agentes ativos; o primeiro é o primário. |
| `--threads N` | Máximo de agentes processados em paralelo por rodada. |
| `--timeout N` | Timeout em segundos para execução de agentes. |
| `--idle-timeout N` | Timeout de inatividade do input. |
| `--interactive-test` | Modo de teste interativo para automação. |
| `--test-prompt ...` | Prompt usado no modo de teste. |
| `--theme TEMA` | Tema visual da sessão. |
| `--set-theme TEMA` | Persiste tema padrão e encerra. |
| `--set-history-window N` | Persiste janela de histórico e encerra. |
| `--driver-repl PLUGIN` | Inicia REPL para testar plugin OpenAI-compatible. |
| `--working-dir DIR` | Diretório de trabalho para REPL. |
| `--prompt TEXTO` | Prompt one-shot para REPL. |
| `--connect AGENTE` | Configura conexão persistida. |
| `--base PLUGIN` | Herda comando/formatação de plugin base. |
| `--driver cli|openai` | Define tipo de conexão. |
| `--cmd ...` | Comando CLI para conexão `cli`. |
| `--model MODELO` | Modelo para conexão OpenAI-compatible ou base CLI. |
| `--base-url URL` | Base URL OpenAI-compatible. |
| `--api-key-env VAR` | Variável que contém a API key. |
| `--extra-body JSON` | JSON extra mesclado no corpo de requisição API. |
| `--list-connections` | Lista conexões persistidas. |
| `--no-mcp` | Desativa servidor MCP embutido. |
| `--mcp-socket [PATH]` | Usa socket Unix e opcionalmente define path. |
| `--mcp-http` | Usa MCP HTTP em vez de socket Unix. |
| `--mcp-port N` | Porta HTTP MCP; padrão `9090`. |
| `--mcp-host HOST` | Host HTTP MCP; padrão `127.0.0.1`. |
| `--mcp-token-env VAR` | Variável com token MCP fixo; padrão `QUIMERA_MCP_TOKEN`. |

## Exemplos

### Sessão simples

```bash
quimera
```

### Sessão com agentes específicos

```bash
quimera --agents claude codex --threads 2
```

### Configurar agente OpenAI-compatible

```bash
quimera --connect remoto --driver openai --model modelo \
  --base-url https://api.exemplo/v1 --api-key-env API_KEY_EXEMPLO
```

### Testar driver em REPL

```bash
quimera --driver-repl ollama-granite4 --prompt 'Responda apenas OK'
```
