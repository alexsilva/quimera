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
