# Quimera

Chat multi-agente no terminal que orquestra **Claude** e **Codex** respondendo juntos às suas perguntas. Os dois agentes conversam entre si, podem delegar subtarefas um ao outro e mantêm estado compartilhado ao longo da sessão.

## Como funciona

Por padrão, cada mensagem do usuário aciona dois agentes em sequência:

```
HUMANO → CLAUDE → CODEX
```

Claude responde primeiro. No fluxo normal, o Codex responde em seguida. Se o primeiro agente emitir um handoff via `[ROUTE:...]`, o agente de destino responde à subtarefa e o agente inicial faz uma síntese final.

Os agentes também podem sinalizar que o tópico merece debate mais aprofundado (`[DEBATE]`), ativando um fluxo estendido com mais rodadas de troca. Já os comandos `/claude` e `/codex` desativam a segunda resposta automática, salvo quando houver handoff explícito.

## Pré-requisitos

- Python 3.10+
- CLI do [Claude](https://docs.anthropic.com/en/docs/claude-code) instalada e autenticada (`claude`)
- CLI do [Codex](https://github.com/openai/codex) instalada e autenticada (`codex`)

Ambas as ferramentas precisam estar disponíveis no `PATH`.

## Instalação

```bash
git clone git@github.com:alexsilva/quimera.git
cd quimera
pip install .
```

Ou em modo editável (desenvolvimento):

```bash
pip install -e .
```

## Uso

Navegue até o diretório do seu projeto e inicie o chat:

```bash
cd /caminho/do/seu/projeto
quimera
```

A sessão fica vinculada ao diretório atual. O contexto e o histórico são salvos automaticamente em `~/.local/share/quimera/workspaces/`.

### Comandos disponíveis

| Comando | Descrição |
|---|---|
| `/claude <mensagem>` | Claude responde primeiro (Codex não entra automaticamente) |
| `/codex <mensagem>` | Codex responde primeiro (Claude não entra automaticamente) |
| `/context` | Exibe o contexto persistente do workspace atual |
| `/context edit` | Abre o contexto persistente no editor (`$EDITOR`, ou nano/vim como fallback) |
| `/edit` | Abre o editor para compor uma mensagem longa |
| `/file <caminho>` | Usa o conteúdo de um arquivo como mensagem |
| `/help` | Lista os comandos disponíveis |
| `/exit` | Encerra a sessão e gera o resumo de memória |

### Exemplo de sessão

```
$ quimera
Chat multi-agente iniciado (/exit para sair)

Você: como posso otimizar esta função Python?

[CLAUDE]: ...
[CODEX]: ...

Você: /codex revisa o diff abaixo e aponta problemas

[CODEX]: ...

Você: /exit
```

## Configuração

### Nome do usuário

```bash
quimera --name "SeuNome"
quimera --whoami          # exibe o nome atual
```

### Janela de histórico

Número de mensagens recentes enviadas ao contexto dos agentes na execução atual (padrão: 8):

```bash
quimera --history-window 12
```

### Modo debug

Exibe métricas de prompt (tokens por bloco, modo do protocolo, etc.):

```bash
quimera --debug
# ou
QUIMERA_DEBUG=1 quimera
```

As métricas são salvas em `~/.local/share/quimera/workspaces/<hash>/data/logs/metrics/`.

## Contexto persistente

Cada workspace tem um arquivo de contexto em markdown que é injetado no prompt de cada agente. Use-o para descrever o projeto, convenções de código ou qualquer informação relevante:

```bash
quimera
> /context edit
```

O arquivo fica em `~/.local/share/quimera/workspaces/<hash>/data/context/persistent.md`.

## Memória de sessão

Ao encerrar com `/exit` (ou Ctrl+C), o Quimera pede ao Claude que resuma a conversa e salva o resumo no contexto de sessão. Na próxima execução, o resumo é carregado automaticamente, mantendo continuidade sem reenviar o histórico completo.

Quando o histórico cresce além de 30 mensagens, o resumo automático é acionado sem interromper a sessão.

## Estrutura do projeto

```
quimera/
  app.py        — loop principal, roteamento e protocolo multi-agente
  agents.py     — cliente que executa os processos claude e codex
  prompt.py     — montagem dos prompts enviados a cada agente
  context.py    — leitura/escrita do contexto persistente e de sessão
  storage.py    — log de sessões e snapshot do histórico JSON
  workspace.py  — gerenciamento do diretório de dados por projeto
  config.py     — configurações globais do usuário
  ui.py         — renderização no terminal (via rich)
  constants.py  — constantes e templates de prompt
  cli.py        — entry point (argparse)
```

## Dependências

- [`rich`](https://github.com/Textualize/rich) — renderização markdown e status no terminal

## Licença

Sem licença definida. Uso pessoal e experimental.
