# Tasks, roteamento e review

## Criar uma task

No chat:

```text
/task implemente cache no parser e rode os testes relacionados
```

O Quimera:

1. extrai a descrição;
2. classifica o tipo de task;
3. escolhe um agente elegível considerando capacidades e carga;
4. grava a task em SQLite no workspace;
5. acorda executores em background;
6. atualiza `shared_state` com visão das tasks.

## Tipos de task

| Tipo | Exemplos de palavras-chave |
|---|---|
| `code_edit` | corrija, implemente, edite, refatore, altere, modifique |
| `bug_investigation` | investigue, erro, falha, bug, quebrou, não funciona |
| `test_execution` | execute testes, rode pytest, rodar testes |
| `code_review` | revise, review, code review, inspecione |
| `architecture` | arquitetura, design, protocolo, estratégia |
| `documentation` | documente, README, documentação, docs |
| `general` | fallback quando nenhuma heurística casa |

A classificação também infere complexidade, necessidade de tools, necessidade de edição e nível de risco.

## Scoring de roteamento

Para cada profile elegível, o roteador calcula um score base:

- tier maior soma pontos;
- tipo preferido soma pontos;
- tipo evitado subtrai pontos;
- suporte a edição, longo contexto e tools soma conforme o tipo;
- confiabilidade de tool impacta `test_execution` e `bug_investigation`;
- capacidades específicas (`code_editing`, `bug_investigation`, `documentation`, `architecture` etc.) dão boosts.

Depois aplica balanceamento de carga:

```text
effective_score = base_score - total_de_tarefas_abertas_do_agente
```

As tarefas abertas consideradas são `pending` e `in_progress`.

## Estados da task

| Estado | Significado |
|---|---|
| `pending` | Criada e aguardando execução ou claim. |
| `approved` | Aprovada explicitamente e pronta para execução. |
| `in_progress` | Reservada por um executor. |
| `pending_review` | Execução terminou e aguarda revisão. |
| `reviewing` | Um revisor assumiu a avaliação. |
| `completed` | Concluída com sucesso. |
| `failed` | Falhou sem requeue aplicável. |
| `rejected` | Rejeitada antes ou durante fluxo de aprovação. |
| `proposed` | Proposta aguardando aceite, usada em fluxos compatíveis. |

As transições válidas são controladas centralmente para evitar estados inválidos.

## Review cruzado

Quando há agentes elegíveis além do executor, o Quimera pode selecionar um revisor operacional. A política evita self-review comparando nomes, prefixos e aliases. Se o review falhar e ainda houver candidato, a task pode voltar para `pending` com o agente falho registrado para não repetir a mesma atribuição.

## Failover

Uma task pode ser reatribuída quando:

- o agente executor falhou;
- há outro agente ativo elegível;
- o repositório confirma que a task ainda pode ser assumida por outro candidato.

O histórico de agentes falhos fica na própria task, junto com contador de tentativas.

## Inspecionar tasks por ferramenta

Agentes com MCP podem chamar:

- `list_tasks` para filtrar tasks;
- `list_jobs` para listar jobs de sessão;
- `get_job` para detalhes de um job.

No prompt, o Quimera injeta uma visão resumida das tasks abertas e resultados concluídos quando disponível.
