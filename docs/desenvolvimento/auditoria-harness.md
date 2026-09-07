# Mapa e auditoria do harness

Esta revisão cobre a infraestrutura que conecta input, agentes, ferramentas e
tarefas no Quimera. O foco das correções é concorrência no executor, cancelamento
durante retries e persistência de configuração. O inventário abaixo serve como
mapa de manutenção; não representa uma verificação exaustiva de cada módulo.

## Inventário funcional

Os caminhos de código são relativos à raiz do repositório.

| Área | Pontos de entrada e responsabilidades | Testes de referência |
|---|---|---|
| CLI e bootstrap | `quimera/cli.py` interpreta flags, configura workspace, profiles e MCP; `quimera/app/bootstrap/wiring.py` compõe os serviços. | `test_cli_branches.py`, `test_bootstrap_wiring.py`, `test_cli_interactive_startup.py` |
| Input e TUI | `quimera/ui/textual/` conecta widgets, eventos e renderer; `TextualInputGate` recebe input interativo e `quimera/app/simple_input_gate.py` atende pipes. Aprovações e seleções usam os helpers do gate. | `test_textual_ui.py`, `test_app_inputs.py`, `test_approval_integration.py` |
| Chat e agentes | `quimera/app/chat_processor.py`, `chat_round.py` e `dispatch.py` coordenam turnos; `quimera/agents/client.py` chama subprocessos ou drivers; `agent_call_service.py` aplica retries. | `test_chat_round.py`, `test_app_dispatch.py`, `test_agents.py`, `test_agent_call_service.py` |
| Profiles e conexões | `quimera/profiles/base.py` define capacidades, conexões CLI/API e overrides; `quimera/connection_configurator.py` configura agentes. | `test_profiles_base.py`, `test_connection_configurator.py`, `test_agent_capabilities.py` |
| Tarefas e review | `quimera/tasks/services.py`, `router.py` e `planning.py` classificam/roteiam; `executor.py` reserva e despacha; `runner.py`, `reviewer.py` e `failover.py` processam resultados. `repository.py` persiste estados e dependências em SQLite. | `test_runtime_task_executor.py`, `test_task_repository.py`, `test_task_router.py`, `test_anti_self_review.py`, `test_stage5_workflow.py` |
| Ferramentas e política | `quimera/runtime/executor.py` registra e executa ferramentas; `policy.py`, `approval.py` e `approval_broker.py` coordenam permissões e input. `runtime/tools/` contém arquivos, shell, Git, web, browser, memória, tasks e delegação. | `test_runtime_executor.py`, `test_runtime_policy.py`, `test_runtime_approval.py`, `test_tool_executor_fork.py` |
| MCP | `quimera/runtime/mcp/server.py` e `http_server.py` expõem ferramentas; `client.py` conecta servidores externos e `manager.py` gerencia conexões da sessão. | `test_runtime_mcp_server.py`, `test_mcp_http_server.py`, `test_mcp_client_bridge.py`, `test_fake_agents.py` |
| Persistência e diagnóstico | `quimera/workspace.py` separa dados por workspace; `storage.py`, `shared_state.py`, `workspace_memory.py` e `evidence/` mantêm histórico/contexto. `config_store.py` centraliza atualizações JSON de configuração. | `test_workspace.py`, `test_storage.py`, `test_shared_state_ttl.py`, `test_evidence_store.py`, `test_config_store.py` |

## Fluxos operacionais

O chat passa pelo gate de input, resolução de comando/agente, montagem de prompt,
driver e eventual execução de ferramentas. O resultado alimenta renderer,
histórico e métricas. Sem terminal interativo, a CLI executa o loop com
`SimpleInputGate`; com terminal, usa a aplicação Textual.

Uma task passa por classificação e roteamento antes de ser persistida. O roteador
combina score de capacidade com carga aberta; editar código e executar testes são
capacidades distintas. O executor consulta seu gate e capacidade disponível antes
de reservar uma task no repositório. A execução pode concluir, falhar ou enviar o
resultado para review; o revisor é reservado separadamente e não pode ser o autor
da execução. Dependências incompletas impedem a reserva de tarefas dependentes.

No caminho MCP, `tools/call` chega ao executor de ferramentas e à política de
aprovação. A ferramenta `delegate` resolve outro agente e usa o dispatch de
background. Conexões MCP externas publicam suas ferramentas no registry local.
Os detalhes desses caminhos estão em [Fluxos de execução](fluxos.md).

## Problemas corrigidos

### Reserva de tarefas e lifecycle dos workers

O executor podia reservar mais tarefas do que o pool conseguia executar, deixando
trabalho em `in_progress` na fila interna. Agora a consulta de capacidade, a
reserva e a submissão são serializadas, inclusive quando o polling manual compete
com o loop em background. Execução e review usam o mesmo limite de workers.

A conclusão de um worker acorda o poller, sem exigir esperar todo o intervalo de
polling. `stop()` bloqueia novas reservas; reiniciar enquanto workers anteriores
continuam ativos gera erro explícito. O intervalo deve ser finito e positivo e o
número de workers deve ser um inteiro positivo.

Exceções não tratadas pelos handlers são registradas e encaminhadas para falha se
a tarefa carregada ainda estiver em execução/review pelo agente. Uma tarefa já
concluída ou carregada com outro responsável não é marcada como falha por esse
caminho de recuperação. Falhas na própria recuperação também são registradas.

O campo `reviewed_by`, já persistido no SQLite, passou a integrar `TaskRecord` e a
consulta do repositório. Isso permite ao executor conferir o revisor antes do
despacho; o teste com SQLite valida reserva, carregamento e conclusão do review.

### Cancelamento durante retries

As esperas entre tentativas verificam cancelamento em intervalos de até 100 ms.
Uma resposta recebida após cancelamento é descartada antes de registrar sucesso
ou falha. Exceções de comunicação também respeitam o backoff de rate limit e o
`retry_after` fornecido pelo driver; valores não finitos ou inválidos deste último
usam o atraso padrão. O cancelamento não penaliza o agente no rastreador de falhas.

### Configurações e conexões

`config_store.py` grava um arquivo temporário no mesmo diretório, faz flush/fsync
e substitui o destino com `os.replace`. Atualizações de leitura/modificação/escrita
usam lock por thread e `flock` por processo para preservar alterações concorrentes.
Uma falha de serialização ou substituição mantém o arquivo anterior.

JSON ilegível, inválido ou com raiz diferente de objeto produz aviso sem incluir
seu conteúdo. Leituras usam defaults; atualizações recusam sobrescrever esse
arquivo até ele ser corrigido. Valores booleanos não contam como limites inteiros
de histórico ou timeout. Entradas de conexão com nome ou estrutura externa
inválida são ignoradas na leitura.

Overrides de agentes só mudam em memória após persistência bem-sucedida. A
referência ao profile base é preservada também no formato legado de string.
Servidores MCP e suas variáveis de ambiente são salvos juntos, tanto na CLI quanto
no manager usado pela TUI. A formatação de conexões com `extra_body` continua
serializando esse campo como JSON.

## Validação

Os testes de regressão cobrem concorrência entre threads e processos, falha de
gravação, configuração corrompida, cancelamento de retries, capacidade do executor,
parada/reinício, recuperação de handlers e despacho de review com SQLite.

Validação local em 7 de setembro de 2026, com Python 3.13:

| Verificação | Resultado |
|---|---|
| Suíte completa (`.venv/bin/python -m pytest -q --tb=short --basetemp=DIR_TEMPORARIO_NOVO tests`) | 3.424 aprovados, 9 ignorados e 2 falhas em expectativas antigas de backoff; 199,52 s. Inclui integrações com agentes fake e testes automatizados da TUI. |
| Reexecução de `tests/test_app_dispatch.py` após ajustar as duas expectativas | 61 aprovados; 3,31 s. Os testes verificam o atraso total de 0,5 s e intervalos de até 0,1 s. |
| Executor, repositório e cobertura complementar após os ajustes de review | 85 aprovados; 7,77 s. |
| Documentação (`mkdocs build --strict`) | Aprovada, com saída em diretório temporário. |
| Integridade dos patches (`git diff --check`) | Sem erros. |

A revalidação dos testes de dispatch resolve as duas falhas da rodada completa;
depois dela, somente testes e documentação foram ajustados. Não houve uma segunda
execução integral. Use um diretório temporário novo para `--basetemp`, pois pytest
pode remover o conteúdo de um diretório reutilizado. A execução com diretório
isolado também evita a limpeza de resíduos antigos de outras rodadas do ambiente.

Para reproduzir os cenários centrais:

```bash
pytest -q tests/test_config_store.py tests/test_agent_call_service.py tests/test_runtime_task_executor.py tests/test_task_repository.py
pytest -q tests/test_fake_agents.py tests/test_mcp_client_bridge.py tests/test_textual_ui.py
pytest -q tests
mkdocs build --strict
```

Para smoke manual com agentes locais, use `--test`; o backend fake é iniciado
automaticamente. O nome canônico do agente de delegação é `fake-cli-delegate`:

```bash
python quimera.py --test --agents fake-cli-delegate fake-openai --visibility full
```

Consulte os gatilhos e evidências esperadas em [Testes e qualidade](testes.md).

## Limites desta revisão

- Testes fake exercitam protocolo e integração local; autenticação, quotas e
  comportamento de provedores reais dependem de validação no ambiente de uso.
- A validação automatizada de TUI não substitui observar renderização e input em
  cada terminal suportado.
- `stop()` impede novas reservas, mas não interrompe à força um handler já em
  execução; o cancelamento de agentes/processos pertence ao lifecycle superior.
- O lock de configuração depende de `fcntl.flock` (POSIX) e da cooperação dos
  escritores. Edições externas e substituições integrais não oferecem merge.
- O salvamento conjunto das listas MCP não é uma transação com o handshake
  remoto, nem mescla edições concorrentes feitas a partir de snapshots antigos.
- A escrita atômica adicionada cobre configurações e conexões; outros arquivos
  de histórico, memória e metadados mantêm seus mecanismos próprios.
