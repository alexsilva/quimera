# Visão geral

## O que o Quimera faz

O Quimera organiza uma sessão de engenharia assistida por múltiplos agentes. O usuário conversa no terminal, escolhe agentes por prefixo, cria tasks explícitas, libera ou bloqueia ferramentas, acompanha outputs resumidos ou completos e mantém memória operacional por workspace.

As funcionalidades centrais são:

1. **Chat multiagente**: cada agente é um plugin com prefixo, comando, driver, capacidades e metadados de roteamento.
2. **Roteamento explícito**: mensagens podem ir para o agente primário ou para um agente escolhido com `/claude`, `/codex`, `/gemini`, `/opencode` etc.
3. **Modos de execução**: `/planning`, `/analysis`, `/design`, `/review` e `/execute` mudam o conjunto de ferramentas bloqueadas no turno.
4. **Tasks em background**: `/task <descrição>` classifica, persiste, atribui e acorda executores para trabalhar fora do turno principal.
5. **Review cruzado e failover**: tasks podem passar por revisão de outro agente e voltar para fila se falharem.
6. **Runtime de ferramentas**: leitura/escrita de arquivos, patch, shell, web, TODOs, tasks e handoff são expostos para agentes.
7. **MCP embutido**: agentes compatíveis recebem o runtime do Quimera como servidor MCP por socket Unix ou HTTP.
8. **Estado e memória por workspace**: histórico, contexto persistente, resumo de sessão anterior, logs, métricas, banco SQLite de tasks e evidências ficam isolados por diretório de projeto.

## Componentes principais

| Área | Responsabilidade |
|---|---|
| `quimera/cli.py` | Parse de flags, configuração inicial, seleção de agentes, inicialização MCP e bootstrap da app. |
| `quimera/app/` | Loop interativo, comandos slash, roteamento, sessão, execução de turnos, tasks e renderização de eventos. |
| `quimera/plugins/` | Catálogo de agentes, conexões CLI/API, injeção MCP e metadados de capacidade. |
| `quimera/runtime/` | Drivers, schemas de ferramentas, executor, parser, políticas, MCP e execução de tasks. |
| `quimera/runtime/tools/` | Implementações de ferramentas: arquivos, shell, patch, handoff, tasks, web e TODO. |
| `quimera/ui/` | Renderer terminal, temas e auditoria visual. |
| `quimera/evidence/` | Modelos, parsing, formatação e armazenamento de evidências. |
| `quimera/workspace.py` | Layout persistente por workspace e diretórios temporários. |

## Fluxo macro de uma sessão

```text
Usuário inicia `quimera`
  -> CLI carrega configuração, plugins e workspace
  -> app inicia renderer, session state, logs e agentes ativos
  -> MCP embutido é iniciado, salvo `--no-mcp`
  -> usuário envia mensagem ou comando slash
  -> CommandRouter resolve modo/agente
  -> AgentClient ou driver executa o agente
  -> runtime processa tools, aprovações, handoffs e estado
  -> respostas, eventos, métricas e contexto são persistidos
```

## Filosofia operacional

O Quimera não tenta esconder a execução. Ele privilegia controle local, auditabilidade e composição explícita: o usuário vê qual agente foi usado, quais tools foram chamadas, quando uma task foi criada, por que um agente foi escolhido e onde os dados ficaram gravados.
