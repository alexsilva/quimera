# Referência de comandos slash

## Comandos internos

| Comando | Aliases | Descrição |
|---|---|---|
| `/help` | `/g` | Mostra ajuda. |
| `/agents` | — | Lista agentes ativos. |
| `/connect <agente>` | — | Configura conexão pelo chat. |
| `/disconnect <agente>` | — | Remove conexão persistida. |
| `/reload` | — | Recarrega profiles/conexões. |
| `/prompt [agente]` | — | Mostra preview do prompt final. |
| `/context` | `/r` | Mostra contexto. |
| `/context-edit` | — | Edita contexto persistente. |
| `/context-branch <nome>` | — | Seleciona branch de contexto. |
| `/edit` | `/e` | Abre editor para mensagem longa. |
| `/file <caminho>` | — | Envia conteúdo de arquivo como mensagem. |
| `/task <descrição>` | — | Cria task humana explícita. |
| `/bugs ...` | — | Acessa serviços de bugs. |
| `/stats ...` | — | Consulta métricas de comportamento dos agentes. |
| `/approve` | `/a`, `/y` | Pré-aprova próxima mutação. |
| `/approve-all` | `/aa` | Autoaprova mutações subsequentes. |
| `/reset [state\|history\|all]` | — | Limpa `shared_state`, histórico ou ambos. |
| `/clear` | — | Limpa tela. |
| `/exit` | — | Encerra chat. |

## Modos

| Comando | Efeito |
|---|---|
| `/planning` | Ativa modo de planejamento. |
| `/analysis` | Ativa modo de análise. |
| `/design` | Ativa modo de design. |
| `/review` | Ativa modo de revisão. |
| `/execute` | Remove restrições de modo. |

## Prefixos de agentes

Prefixos dependem dos profiles ativos. Os padrões são:

- `/claude`
- `/codex`
- `/gemini`
- `/opencode`
- `/ollama-granite4`

Agentes dinâmicos criados por `--connect meu-agente` recebem prefixo `/<nome>`.

## `/bugs`

O serviço de bugs aceita subcomandos como `list`, `show`, `close`, `analyze` e `stats`. Use autocomplete ou `/bugs list` para descobrir o estado atual do registro.

## `/stats`

As métricas de entrega dos agentes (latência, respostas vazias, próximo passo claro, delegações, uso de ferramentas) são coletadas e persistidas em `<workspace>/state/metrics_state.json`, mas **não são injetadas no prompt dos agentes**. `/stats` apenas expõe os dados coletados — não há camada de diagnóstico, alerta ou feedback:

| Comando | Efeito |
|---|---|
| `/stats` | Resumo de uma linha por agente. |
| `/stats <agente>` | Detalhamento completo das métricas do agente. |
| `/stats json [<agente>]` | Resumo bruto em JSON. |
| `/stats reset [<agente>]` | Zera as métricas de um agente ou de todos, atualizando `metrics_state.json`. |
