# Referência de CLI

A CLI é exposta pelo script `quimera` configurado em `pyproject.toml`.

## Uso geral

```bash
quimera [opções] [test_agent]
```

## Flags principais

| Flag | Descrição |
|---|---|
| `--version` | Exibe a versão derivada do Git e encerra. |
| `--name NOME [NOME ...]` | Define nome do usuário na sessão. |
| `--whoami` | Mostra identificação/configuração relacionada ao usuário. |
| `--debug` | Ativa métricas e auditoria de renderização em logs. |
| `--history-window N` | Define janela de histórico desta sessão. |
| `--visibility quiet|summary|full` | Controla detalhe da execução em todos os drivers/perfis: `quiet` mantém tools e erros visíveis, `summary` mostra o resumo operacional e `full` inclui a saída completa; sem a flag, vale o valor salvo na config. |
| `--agents AGENTE [AGENTE ...]` | Lista agentes ativos desta sessão; o primeiro é o primário. Sem a flag, vale a seleção salva na config (ou todos). |
| `--threads N` | Máximo de agentes processados em paralelo por rodada nesta sessão; sem a flag, vale o valor salvo na config. |
| `--idle-timeout N` | Timeout de inatividade do input (sem stdout do agente). Padrão: 180s. |
| `--set-idle-timeout N` | Persiste idle timeout padrão na config e encerra. |
| `--interactive-test` | Modo de teste interativo para automação. |
| `test_agent` | Agente alvo para o modo de teste (usado com `--interactive-test`). |
| `--test-prompt ...` | Prompt usado no modo de teste. |
| `--test` | Ativa modo de teste: somente profiles fake entram na rodada. |
| `--theme TEMA` | Tema visual da sessão. |
| `--set-theme TEMA` | Persiste tema padrão e encerra. |
| `--set-history-window N` | Persiste janela de histórico e encerra. |
| `--driver-repl PERFIL` | Inicia REPL interativo para testar um profile openai_compat. |
| `--working-dir DIR` | Diretório de trabalho para o REPL (padrão: cwd). |
| `--prompt TEXTO` | Prompt one-shot para `--driver-repl` (modo não-interativo). |
| `--connect AGENTE` | Configura interativamente a conexão de um agente e persiste. |
| `--profile PERFIL` | Perfil de execução para herdar cmd/output_format (ex: opencode). |
| `--driver cli|openai` | Define tipo de conexão. |
| `--cmd ...` | Comando CLI para conexão `cli`. |
| `--model MODELO` | Modelo para conexão OpenAI-compatible ou base CLI. |
| `--base-url URL` | Base URL OpenAI-compatible. |
| `--api-key-env VAR` | Variável que contém a API key. |
| `--extra-body JSON` | JSON extra mesclado no corpo de requisição API. |
| `--list-connections` | Lista conexões persistidas. |
| `--no-mcp` | Desativa servidor MCP embutido. |
| `--mcp [PATH]` | Atalho para `--mcp-socket`. |
| `--mcp-socket [PATH]` | Ativa MCP via socket Unix e opcionalmente define path do socket. |
| `--mcp-http` | Expõe servidor MCP HTTP adicional; agentes locais continuam usando socket Unix interno. |
| `--mcp-port N` | Porta HTTP MCP; padrão `9090`. |
| `--mcp-host HOST` | Host HTTP MCP; padrão `127.0.0.1`. |
| `--mcp-http-allow-tools CSV` | Allowlist de tools para MCP HTTP externo (padrão: read). |
| `--mcp-client nome=transporte:endpoint` | Conecta a um servidor MCP externo e expõe suas tools com prefixo `nome_`. Aceita `remote:` (OAuth via `mcp-remote`), `stdio:`, `socket:` e URLs `http(s)://`. Persistido por workspace em `mcp-connections.json`; pode ser repetido. |
| `--mcp-client-env nome=KEY=val,...` | Variáveis de ambiente para uma conexão MCP client. |

As conexões `--mcp-client` não bloqueiam a inicialização: o handshake de cada
servidor roda em background depois que a interface sobe. O bloco de status no
início do chat mostra cada conexão como aguardando, conectando, conectada (com a
quantidade de tools) ou com falha (com o erro). Uma autorização OAuth pendente
(`remote:`) ganha uma linha com a URL completa, clicável, e o navegador é aberto
automaticamente (desligue com `QUIMERA_OPEN_BROWSER=0`). Assim que uma conexão
completa, suas tools passam a aparecer no `tools/list` do MCP embutido e ficam
disponíveis aos agentes. Conexões com falha ou presas podem ser refeitas no MCP
Hub (`F9`) sem travar a interface.

## Exemplos

### Sessão simples

```bash
quimera
```

### Consultar versão

```bash
quimera --version
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
