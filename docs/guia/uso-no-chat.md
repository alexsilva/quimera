# Uso no chat

## Mensagens comuns

Se a entrada não começa com comando interno nem prefixo de agente, ela vai para o agente primário da sessão. O primário é o primeiro item em `--agents`; se nenhum for informado, o padrão histórico é `claude`.

```text
Explique a arquitetura deste módulo e sugira um plano de refatoração.
```

## Enviar para um agente específico

Use o prefixo do plugin:

```text
/codex rode os testes relacionados a tasks
/claude revise a estratégia antes de editar
/gemini proponha uma arquitetura para persistência SQLite
/opencode ajuste este arquivo pequeno
```

O roteador impede prefixos duplicados na mesma mensagem, como `/claude /codex ...`, para evitar ambiguidade.

## Modos de execução

Os modos são comandos que ajustam restrições do turno. Podem ser usados sozinhos ou com mensagem na sequência.

| Modo | Uso típico |
|---|---|
| `/planning` | Planejamento sem execução prática. |
| `/analysis` | Leitura e análise. |
| `/design` | Desenho técnico sem alteração de código. |
| `/review` | Revisão sem edição. |
| `/execute` | Remove restrições de modo e libera execução normal. |

Exemplos:

```text
/planning como migrar o runtime MCP para outro transporte?
/review revise o patch atual sem alterar arquivos
/execute implemente a correção e rode os testes
```

## Compor mensagens longas

- `/edit` ou `/e` abre o editor externo para compor a mensagem.
- `/file <caminho>` lê o conteúdo de um arquivo e envia como mensagem.

## Aprovação de mutações

O runtime diferencia ferramentas de leitura e de mutação. Quando a política exige aprovação:

- `/approve` ou `/a` pré-aprova a próxima mutação;
- `/approve-all` ou `/aa` aprova mutações subsequentes automaticamente.

Use com cuidado: agentes com ferramentas podem editar arquivos e executar comandos permitidos.

## Contexto e prompt

- `/context` mostra contexto persistente/sessão.
- `/context-edit` edita o contexto persistente.
- `/context-branch <nome>` separa contexto por branch lógica.
- `/prompt [agente]` mostra prévia do prompt final para depuração.

## Limpar ou sair

- `/clear` limpa a tela.
- `/reset [state|history|all]` limpa `shared_state`, histórico ou ambos.
- `/exit` encerra a sessão.
