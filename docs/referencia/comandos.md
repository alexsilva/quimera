# Referência de comandos slash

## Comandos internos

| Comando | Aliases | Descrição |
|---|---|---|
| `/help` | `/g` | Mostra ajuda. |
| `/agents` | — | Lista os agentes do pool; um agente suspenso por falhas seguidas aparece com o motivo e quando volta. |
| `/stop [#número\|agente]` | — | Para somente a execução de chat escolhida; sem alvo, para a única ativa ou lista as opções. |
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
| `/clear` | — | Limpa o chat; mantém o cabeçalho da sessão (logo, projeto, MCP, logs). |
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

## `/stop` e slots paralelos

Na TUI, o bloco `⚡ ativos/capacidade` da barra inferior é clicável e `F7` abre o mesmo menu pelo teclado (`F7` ou `Esc` fecham). O menu mostra os prompts que ocupam slots e os que aguardam na fila; selecione uma linha e use **Parar selecionado** (ou a tecla `s`). Fora da TUI, `/stop` oferece a mesma operação:

Ainda na TUI, o próprio `/stop` tem autocomplete dinâmico: ao digitar `/stop ` o dropdown lista as execuções em voo no formato `#n agente · tempo · trecho do prompt`; `Enter` ou `Tab` inserem só o número (`/stop #2`) e o nome do agente filtra a lista (`/stop cod` mostra as execuções de `codex-gpt-5-6-sol`). Não é preciso saber o nome exato nem o número de cabeça.

| Comando | Efeito |
|---|---|
| `/stop` | Para a única execução existente; se houver várias, lista os números com agente, tempo decorrido e trecho do prompt. |
| `/stop #2` | Para exatamente a execução `#2`, inclusive se o mesmo agente estiver em mais de um prompt. |
| `/stop codex` | Para o agente cujo nome é `codex` ou começa por `codex` (ex.: `codex-gpt-5-6-sol`) quando isso identifica uma única execução; se houver mais de uma, pede o número. |

A parada marca a submission inteira como cancelada, portanto não dispara failover para outro agente e não conta como falha. As outras execuções continuam. `Ctrl+C` conserva o comportamento global existente.

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
