# Workflows distribuídos

Um workflow é um objetivo do usuário decomposto pelo agente principal em tasks
com dependências, publicado no banco e executado pelo runtime com os executores,
o roteador e a revisão que já existem para `/task`. O agente principal continua
responsável pelo objetivo; o runtime distribui, acompanha e o consulta só quando
há uma decisão a tomar.

Para trabalho simples, o agente resolve sozinho ou usa `tasks_delegate`. O
workflow é a opção para trabalho amplo, com etapas dependentes ou
paralelizáveis. O prompt do chat apenas indica essa opção; nada obriga a criar
um workflow.

## Componentes

| Responsabilidade | Módulo | Observação |
|---|---|---|
| Contrato de work item (comum com `/debate`) | `quimera/domain/work_items.py` | `WorkItem`, parsing estrito, IDs, ciclos |
| Modelos e estados | `quimera/workflows/models.py` | `WorkflowStatus`, transições, `WorkflowLimits`, snapshot |
| Planejamento | `quimera/workflows/plan.py` | validação determinística, hash, `writes` |
| Persistência | `quimera/workflows/repository.py`, `schema.py` | único lugar com SQL de workflows |
| Escalonamento | `TaskRepository.claim_task` + `EXEC_CLAIM_GATE_SQL` | o claim existente ganha o gate de workflow |
| Execução/revisão | `TaskRunner`/`TaskReviewer` + `workflows/execution.py` | ganchos só para `origin == "workflow"` |
| Handoff | `quimera/workflows/handoff.py`, `context.py` | resultado estruturado e contexto da dependente |
| Replanejamento | `quimera/workflows/replan.py` | o que preservar entre versões |
| Coordenação | `quimera/workflows/coordinator.py` | reconciliação determinística e consulta ao principal |
| Ciclo de vida | `quimera/workflows/service.py` | monitor, recuperação na partida, consulta |
| Tools | `quimera/runtime/tools/workflows.py` | camada fina, sem regra de negócio |
| UI | `quimera/workflows/presentation.py`, `commands.py` | `/workflow` e `shared_state.workflow_overview` |

O `AppTaskServices` cria o `WorkflowService` sob demanda. Ele é iniciado junto
com os executores de task (`setup_task_executors`) e parado com eles. Só uma
sessão do Quimera coordena os workflows de um workspace: a que detém o
`CoordinationLock` (`flock` em `tasks.workflows.lock`, ao lado do `tasks.db`,
liberado pelo sistema quando o processo morre). Uma segunda sessão no mesmo
workspace não adota, não reconcilia e recusa `workflows_*` e `/workflow` com
erro explícito; quando o lock fica livre, ela assume e faz a recuperação. O
`TaskExecutorPool` injeta os ganchos de workflow no `TaskRunner` e no
`TaskReviewer` e registra as execuções vivas por task, para que o cancelamento
alcance só as execuções do workflow cancelado.

## Persistência

Tudo fica no `tasks.db` do workspace:

- `workflows`: objetivo, coordenador, estado, versão vigente, critérios globais,
  restrições, contadores de replanejamento e de consultas, `publication_key`
  única (idempotência);
- `workflow_plans`: cada versão do plano, com JSON canônico e hash (histórico);
- `workflow_tasks`: vínculo item ↔ task, versão, `writes`, evidências
  esperadas, `superseded_in` (task substituída por um replanejamento);
- `task_attempts`: uma linha por tentativa (task, tentativa, agente, run id,
  desfecho, handoff, motivo de falha).

As tasks são linhas normais de `tasks` (`origin = "workflow"`), com as
dependências em `task_dependencies`. A coluna `tasks.attempt_count` já existia e
passou a ser lida em `TaskRecord`.

A publicação ocorre numa única transação `BEGIN IMMEDIATE`: workflow, versão 1,
tasks, vínculos e dependências. O workflow nasce `ready`, e o gate de claim só
libera tasks de workflows em `running`. Nenhum worker vê um plano pela metade. A
`publication_key` é derivada de job, coordenador e hash do plano (ou da
`idempotency_key` informada). Publicar de novo devolve o mesmo workflow.

## Estados

Workflow: `ready → running → (paused | blocked | completed | failed | cancelled)`.
`paused`/`blocked` voltam a `running`. `completed` só reabre por
replanejamento. `failed` e `cancelled` são terminais. As tasks usam os estados
existentes. A máquina de tasks ganhou só transições: para `cancelled` a partir
de `proposed`, `approved`, `pending`, `pending_review` e `reviewing`, e
`pending → failed` (task que nunca poderá rodar).

## Escalonamento

O claim existente (`TaskRepository.claim_task`, com roteamento por
`effective_score`, falhas por agente e dependências concluídas) recebe dois
predicados adicionais (`workflows/schema.py`):

- task de workflow só é reivindicável com o workflow em `running` e se não foi
  substituída;
- task com `writes = 1` não é reivindicada enquanto outra task de escrita de
  workflow estiver em `in_progress`, ou em `pending_review`/`reviewing` num
  workflow que pode revisá-la (`running`/`blocked`). Uma revisão parada num
  workflow pausado não trava os demais. Escritas ficam serializadas e leituras
  continuam paralelas.

A revisão segue enquanto o workflow está `running` ou `blocked`. Uma dependente
só fica pronta quando a predecessora está `completed`, o que inclui a revisão
aceita. Uma revisão rejeitada devolve a task a `pending` (nova tentativa, com
outro agente quando houver) e as dependentes continuam bloqueadas. O requeue
de uma task de workflow (falha de execução ou revisão rejeitada) que esgota
`max_task_attempts` leva a task direto a `failed` na mesma transação, sem
janela para um novo claim.

`writes` vem do plano. Sem o campo, `code_edit` e `documentation` contam como
escrita. Tasks sem escrita rodam com o workspace somente leitura, aplicado pelo
runtime (`read_only_mode`: as mesmas tools bloqueadas do `/analysis` e bwrap
somente leitura quando há sandbox), sem afrouxar o modo já ativo na sessão. A
revisão de uma task sem escrita também roda somente leitura. Não
há worktrees nesta versão: a edição paralela não é suportada, e as tasks de
escrita são serializadas.

## Execução, tentativas e handoff

`WorkflowTaskHooks` adapta o fluxo existente:

- monta o contexto a partir do banco: objetivo, critérios globais, restrições,
  escopo e critérios da task e os handoffs aceitos das dependências diretas,
  delimitados como dados não confiáveis (`<dependency_result untrusted="true">`)
  e truncados com aviso explícito. O histórico do chat não entra;
- registra cada tentativa em `task_attempts` com run id
  `agentrun:wf<W>-t<T>-a<N>-<agente>` (revisão: `-r<N>-`), que também é o
  `run_id` do AgentClient;
- converte a resposta no `Handoff` estruturado (bloco JSON pedido no contexto) e
  o verifica sem IA: arquivos declarados precisam existir no workspace,
  verificações com falha reprovam, a evidência esperada exige verificações ou
  referências, e `partial`/`failed` não concluem. Uma resposta sem bloco não
  conta como evidência quando o plano espera evidência.

Todas as escritas de estado de uma task de workflow levam
`expected_attempt`. Uma tentativa antiga (por exemplo, a que termina depois de
um lease expirado ou de um cancelamento) não sobrescreve o resultado de uma
mais nova, e um resultado que chega após o cancelamento é descartado.

## Coordenação

`WorkflowCoordinator.reconcile` só lê o banco e é idempotente. É chamado pelo
monitor do `WorkflowService`, que acorda com eventos de task e, sem eles, a
cada 5 s:

1. reenfileira tasks em voo sem execução viva neste processo e com lease
   (`lease_seconds`) expirado;
2. falha (`pending → failed`) tasks prontas que nunca poderão rodar (sem agente
   elegível ou com o limite de tentativas atingido) e devolve ao roteador só as
   tasks do workflow atribuídas a agentes que saíram do pool;
3. com tudo concluído, passa o workflow a `completed`; sem nada em execução
   nem pronto, a `blocked` (por exemplo, quando uma dependência falhou de vez,
   ou quando uma task aguarda revisão e não resta no pool agente além do
   executor para revisá-la). Assim, um workflow nunca fica indefinidamente em
   `running`.

A transição de conclusão é um compare-and-set pelo estado e pela versão do
plano lida no snapshot: um replanejamento aplicado entre a leitura e a decisão
a anula (o mesmo vale para `workflows_finish completed`).

Uma task interrompida por cancelamento ou replanejamento não conta como falha
do agente executor.

Nos pontos de decisão (`completed` ou `blocked`), o coordenador consulta o
agente principal uma vez por motivo, até `max_consults`, por uma delegação de
background sem auto-aprovação. O principal verifica as evidências e chama
`workflows_finish` (completed, blocked ou failed) ou `workflows_replan`.

Na partida, `recover()` adota no job atual os workflows abertos de sessões
anteriores, devolve à fila as tasks que estavam em execução (nova tentativa) ou
em revisão, e pausa os que estavam em `running`. Nada é declarado concluído
porque estava em execução antes do reinício. O coordenador retoma com
`workflows_control resume`.

## Replanejamento

`workflows_replan` recebe a lista completa de tasks e a `base_version` vigente.
Na mesma transação, ele:

- grava a nova versão em `workflow_plans`, preservando as anteriores;
- preserva as tasks vivas com especificação idêntica (o responsável sugerido
  não conta) cujas dependências também foram preservadas;
- cancela e marca como superadas (`superseded_in`) as alteradas ou removidas;
  as que estavam em execução são interrompidas;
- cria as tasks novas, ligadas às preservadas.

Reenviar o mesmo plano não tem efeito. Uma `base_version` desatualizada é
recusada, e `max_replans` limita o número de replanejamentos.

## Tools e permissões

| Tool | Aprovação | Quem pode |
|---|---|---|
| `workflows_create` (`dry_run` só valida) | não (só grava em `ready`) | qualquer agente com identidade confiável |
| `workflows_start` | mesma de `tasks_create` | coordenador |
| `workflows_status` | não (leitura) | qualquer agente |
| `workflows_control` (pause/resume) | não | coordenador |
| `workflows_replan` | mesma de `tasks_create` | coordenador |
| `workflows_cancel` | não | coordenador |
| `workflows_finish` | não | coordenador |

As tools aparecem por `tool_search` (não são essenciais) e ficam bloqueadas no
modo debate. Os executores das tasks rodam com a mesma auto-aprovação de tools
das tasks de `/task`, o que só acontece depois que o `workflows_start` passou
pela aprovação de criação de tasks. Dentro de uma task de workflow (run id
`agentrun:wf…`), `workflows_create`, `workflows_start` e `workflows_replan` são
recusadas, o que impede cascatas de workflows com permissão herdada. Um
`tasks_delegate` feito a partir dessa task gera um run filho com o mesmo
prefixo (`agentrun:wf…-d<id>`), então a recusa vale em qualquer profundidade
de delegação. Com o modo orquestrador ativo, um agente delegado pelo
orquestrador também não pode usar essas três tools (mesma regra do
`tasks_delegate`).

O humano usa `/workflow [list|show <id>|pause <id>|resume <id>|cancel <id>]`.

## Limites (`workflow_limits` no `config.json`)

| Chave | Padrão |
|---|---|
| `max_tasks` (por plano) | 20 |
| `max_replans` | 3 |
| `max_consults` (ao agente principal) | 6 |
| `max_open_workflows` (por job) | 3 |
| `max_task_attempts` | 3 |
| `lease_seconds` | 1800 |

Só inteiros positivos são aceitos; o que não for reconhecido é ignorado.

## Limitações conhecidas

- A exclusividade de escrita vale entre tasks de workflow. Tasks de `/task` e
  delegações diretas não participam do gate.
- Sem worktrees, não há edição paralela isolada.
- O modo somente leitura depende de `bwrap` para agentes CLI. Sem sandbox, a
  restrição vale para as tools do Quimera (MCP/nativas), mas não para as tools
  internas do CLI.
- O lease só reenfileira tasks sem execução viva neste processo. A distribuição
  entre processos ou máquinas exigiria lease com dono no banco.
- Só uma sessão por workspace coordena workflows (`CoordinationLock`); as
  demais ficam sem workflows até o lock ficar livre. O `flock` não coordena
  máquinas diferentes sobre um sistema de arquivos de rede.
- Uma execução mais longa que `lease_seconds` pode, numa janela curta entre o
  fim da run e a gravação do resultado, ser reenfileirada. O fence de tentativa
  impede a corrupção do estado, mas o trabalho é refeito.
- A consulta de decisão ao agente principal não é interrompida por um
  cancelamento do workflow; o resultado dela é descartado se o plano mudou.
- Cancelar ou substituir tasks num replanejamento não publica eventos de
  task; o monitor e a UI se atualizam pelo `wake` do coordenador e pelo poll.
