# Quimera: avaliacao, limite atual e roadmap operacional

## Resumo executivo

O Quimera ja tem uso pratico real, mas em um nicho claro: usuarios avancados que querem orquestrar multiplos agentes no terminal com regras explicitas de roteamento, handoff, review e contexto compartilhado por workspace.

O limite principal hoje nao e falta de inteligencia dos agentes. E falta de confiabilidade operacional. Mesmo com supervisao humana, o sistema ainda pode quebrar porque os agentes validam mais codigo e testes locais do que comportamento real do app em execucao.

A evidencia mais forte disso e pratica: quando foram introduzidos logs de chat e mais contexto observavel no debug, a qualidade da atuacao dos agentes melhorou de forma perceptivel. Isso aponta para a direcao correta: menos autonomia cega, mais observabilidade e reproducao operacional.

## O que o Quimera ja tem de valor real

- Orquestracao local e explicita de multiplos agentes.
- Regras claras de roteamento por capacidade e tipo de tarefa.
- Handoff e review como parte do fluxo de trabalho.
- Contexto compartilhado por sessao e workspace.
- `evidence_context` e compartilhamento de dados de execucao no prompt.
- Controle local, auditabilidade e operacao em terminal.

Na pratica, o Quimera ja funciona como um orquestrador experimental forte para engenharia assistida por agentes.

## Onde ele ainda perde

Comparado ao mercado, o Quimera ainda perde principalmente em produto e confiabilidade operacional.

- UX e onboarding.
- Isolamento forte por task.
- Integracoes maduras com GitHub, IDE e servicos externos.
- Teste de comportamento real do app.
- Rollback automatico.
- Seguranca para automacao mais agressiva.

Isso significa que hoje ele esta mais proximo de uma infraestrutura poderosa de coordenacao do que de um sistema pronto para autoevolucao confiavel.

## O problema central do self-update

Hoje o Quimera consegue evoluir o proprio codigo com ajuda dos agentes, mas ainda nao se mantem sozinho de forma segura.

O motivo nao e filosofico. E operacional:

- contexto ainda pode chegar incompleto;
- patches podem estar corretos localmente e errados sistemicamente;
- testes nem sempre reproduzem o uso real;
- mudancas ainda podem quebrar areas vizinhas;
- faltam protecoes fortes antes de aceitar automacao maior.

Em resumo: automatizar mais agora aumentaria a velocidade de regressao.

## Tese principal

O proximo salto do Quimera nao depende principalmente de agentes "mais inteligentes".

Depende de agentes que consigam testar como um humano testa:

- rodando o app de verdade;
- observando logs, render, toolbar, handoffs e estado;
- reproduzindo o cenario real;
- comparando comportamento esperado vs observado;
- trabalhando com evidencia concreta, nao so com leitura de codigo.

## O que melhorou e por que

A introducao de logs de chat no debug melhorou o desempenho dos agentes porque reduziu cegueira operacional.

Isso mostra que o ganho real veio de:

- mais observabilidade;
- melhor contexto para diagnostico;
- maior capacidade de reproduzir sintomas;
- menos inferencia no escuro.

Ou seja: o caminho mais promissor e instrumentacao e replay, nao soltura irrestrita.

## Roadmap recomendado

### Fase 1: Ver primeiro

- Replay de sessao.
- Debug bundles com `render`, `ansi`, metricas, evidencias e eventos.
- Asserts de comportamento como `slots`, handoff e `evidence_context`.
- Probes operacionais para consultar estado real.

### Fase 2: Reproduzir e bloquear regressao

- Smoke tests obrigatorios com o app rodando.
- Comparacao before/after de logs e comportamento.
- Worktree ou sandbox por task.
- Gates por area critica.

### Fase 3: Automatizar com seguranca

- Geracao de testes a partir de sessoes reais.
- Score de confianca por mudanca.
- Rollout supervisionado.
- Rollback automatico quando o comportamento divergir.

## Ampliacao da capacidade de teste

O debate com outros agentes convergiu em um ponto importante: o proximo ganho grande nao vem de "mais modelo", e sim de transformar o que o app ja observa em teste operacional reproduzivel.

### Linha mestra

Fechar o ciclo:

- observar;
- reproduzir;
- comparar;
- bloquear;
- liberar.

### Ideias de maior valor

- Sessao canonica reexecutavel.
  Cada sessao deve poder virar um "cenario observavel" com comandos, inputs, timestamps relativos, handoffs, mudancas de toolbar, probes esperados e invariantes operacionais.
- Replay orientado por cenario, nao so por log.
  O replay precisa validar comportamento como `slots`, `handoff`, `tool_call`, `evidence_context`, slash commands e ordem dos eventos relevantes.
- Asserts sobre transicoes, nao so estado final.
  Em vez de testar so `slots:2/2`, validar sequencias como `0/2 -> 1/2 -> 2/2 -> 1/2 -> 0/2` com janela de tolerancia.
- Oraculo comportamental.
  Reduzir logs e eventos a fatos estaveis como `slot_peak`, `handoff_count`, `evidence_context_present`, `task_completed`, `redisplay_latency_p95`.
- Snapshots semanticos do runtime.
  Expor dumps estruturados por turno com `inflight`, `queue_size`, `active_agents`, `last_handoffs`, `tool_calls` e `evidence_written`.
- Diff comportamental tolerante a ruido.
  Ignorar IDs efemeros, pequenas variacoes de tempo e ordem irrelevante, mas falhar quando sumirem handoff, fila, evidence ou transicoes esperadas.
- Debug bundle executavel.
  O bundle ideal nao e so coleta. Ele deve incluir `manifest.json`, `scenario.json`, `expected.json`, `render.jsonl`, `ansi`, `metrics.jsonl`, `evidence.jsonl`, metadados do workspace e um replay de um comando.
- Corpus de sessoes problema.
  Cada bug real deve virar cenario de regressao obrigatorio para manter memoria operacional do produto.
- Testes por mutacao operacional.
  Injetar falhas controladas em replay, como redraw atrasado, fila nao publicada, evidence vazia, tool call truncada e agente lento.
- Evidence score por utilidade.
  Parar de perguntar apenas "gerou evidencia?" e passar a avaliar se ela e utilizavel por outro agente.
- Modo espelho do operador.
  Deixar o sistema observar uma sessao humana real e extrair quais sinais ALEX usa para confiar que o app esta certo.

### MVP sugerido

- Probes JSON com schema estavel:
  `/debug/slots`, `/debug/handoffs`, `/debug/evidence`, `/debug/queue`, `/debug/session`, `/debug/turns`.
- `BehaviorOracle` lendo `render.jsonl` e probes e convertendo isso em fatos estaveis.
- `parallel smoke` com asserts de slot, fila, handoff e redisplay.
- `DebugBundle` com `manifest + replay`.
- `BehaviorDiff` para comparacao before/after no CI.

### Componentes recomendados

- `ScenarioRecorder`
- `ScenarioRunner`
- `BehaviorOracle`
- `BehaviorDiff`
- `DebugBundle`
- `RolloutGate`

### Gates por risco

- Risco baixo: unit + lint.
- Risco medio: unit + lint + smoke de interacao.
- Risco alto: replay de cenario + diff comportamental + bundle anexado.

## Debate consolidado entre agentes

Os outros agentes convergiram em tres pontos:

- A tese principal esta correta: a melhora recente veio de observabilidade e reproducao operacional, nao de autonomia extra.
- O valor real do Quimera hoje existe, mas mais como infra/orquestrador experimental poderoso do que como produto confiavel de automacao.
- Antes de mais automacao, o minimo necessario e: `sandbox/worktree por task`, `smoke tests reais`, `asserts de comportamento observado`, `debug bundle obrigatorio` e `rollback simples`.

Tambem surgiu uma ressalva util no debate:

- observabilidade sozinha nao resolve;
- se o sistema so acumular logs sem curadoria, ele troca cegueira por ruido.

## Segunda rodada de debate: ampliacao da capacidade de teste

Em maio de 2026, ALEX solicitou uma nova rodada de contribuicoes dos agentes locais do Quimera focada em gerar mais dados e ideias para ampliar a capacidade de teste e validacao autonoma. Quatro agentes contribuiram com ideias originais, organizadas abaixo por especialidade.

### Contribuicoes de RING-2-6-1T (Arquitetura)

1. **TestAgent como cidadao de primeira classe na orquestracao**
   Um tipo de agente `test` participa do mesmo ciclo de handoff, review e evidence dos demais agentes. Recebe tasks do tipo `"validate"`, executa probes, compara comportamento observado vs esperado, e produz evidence que alimenta o `RolloutGate`. O sistema de teste nao e um anexo -- usa os mesmos mecanismos de orquestracao (filas, slots, handoff, contexto) que o Quimera ja gerencia.

2. **Contrato comportamental por tipo de agente**
   Cada especialidade (`code_edit`, `architecture`, `bug_investigation`) expoe um contrato formal do que produz: schemas de evidence esperados, transicoes de slot validas, padroes de handoff permitidos, restricoes de tool_call. O oraculo usa esses contratos para gerar asserts automaticamente -- valida aderencia, nao comportamento geral.

3. **Sandbox em camadas progressivas**
   Tres camadas de isolamento: (1) processo isolado por subprocesso para smoke rapido, (2) git worktree + dados sinteticos para replay com diff comportamental, (3) container efemero com probes ativas para mudancas de alto risco. O roteador decide a camada com base no `risk_score` estimado da task.

4. **Observabilidade como substrato unico de teste e operacao**
   Unificar probes, `render.jsonl`, eventos de handoff e tool_calls em um barramento de fatos observaveis que serve tanto o operador humano quanto o sistema de teste. O `BehaviorOracle` e uma view materializada sobre esse barramento. Qualquer melhoria de observabilidade se converte automaticamente em melhoria de capacidade de teste.

5. **Evolucao dirigida por mutacao de sessoes reais**
   `MutationEngine` que, dado um conjunto de sessoes reais, aplica mutacoes controladas (atraso de probe, evidencia vazia, handoff omitido, slot travado) e verifica se o `BehaviorOracle` e o `BehaviorDiff` detectam a anomalia. Cada bug real vira mutacao obrigatoria.

### Contribuicoes de QWEN3-6-PLUS (Codificacao)

1. **Teste de Contrato de Handoff (HandoffContractTest)**
   Middleware no roteador que, a cada `transfer_task`, serializa o `evidence_context` + `task_payload` antes do handoff, deixa o destino processar, e verifica: toda evidencia da origem chegou no destino, nao ha campos orfaos ou `null` inesperados, nao vazaram dados de sessoes alheias. Integracao via decorator em `router.py:_route_task()`.

2. **Fuzzing Controlado de Comandos (FuzzRouter)**
   Gerador que produz variacoes sistematicas de comandos: unicode fora da BMP, JSON com chaves duplicadas, strings de 10MB, `\x00` no meio do comando, argumentos aninhados 100 niveis. Para cada variacao, o teste assere que o Quimera nao crasha, a sessao continua funcional, e o estado da toolbar/fila nao corrompe. Implementacao via `hypothesis` (property-based testing).

3. **Teste de Isolamento Entre Workspaces Concorrentes**
   Dispara 5 tasks simultaneas em workspaces distintos via `asyncio`, cada uma criando arquivos, registrando evidencias e fazendo tool calls. Ao final, verifica que arquivos do workspace A nao aparecem no diretorio do workspace B, a fila de cada sessao contem apenas as tasks proprias, e nao ha race conditions no `evidence_context` compartilhado.

4. **Teste de Degradacao Graciosa Sob Pressao de Contexto**
   Gerador de sessoes sinteticas que produz N turns (50, 100, 200) com evidencias de tamanho crescente (1KB, 10KB, 100KB por turn). Para cada nivel, mede: latencia do roteador nao degrada mais que 2x, nao ha estouro de memoria (<500MB), truncamento de historico remove mensagens mais antigas primeiro.

5. **Snapshot de Toolbar Como Contrato Visual (ToolbarContractTest)**
   Schema JSON para o estado renderizado da toolbar (`toolbar_state.json`) com campos: `current_agent`, `slots_used/total`, `queue_length`, `active_handoffs`, `last_command`. Em cada turno de cenario canonico, gerar snapshot e comparar com referencia usando `BehaviorDiff` com tolerancia a ruido (ignorar timestamps, PIDs).

### Contribuicoes de MINIMAX-M2-5 (Documentacao e comunicacao)

1. **Cartoes de Cenario Vivos (scenario-card.md)**
   Cada agente, ao executar um cenario de teste, gera um `scenario-card.md` no workspace com: pre-condicoes (slots, agentes ativos), passos executados, probes coletadas, resultado esperado vs observado, e assinatura do agente. ALEX consulta com `quimera test:status --scenario <id>`. Outros agentes usam os cartoes para saber o que ja foi coberto.

2. **Relatorio Estruturado de Falha no Workspace**
   Quando um agente detecta uma falha, escreve um JSON em `debug/failures/<timestamp>/` com: componente falho, manifestacao observada, gravidade, evidencia crua e sugestao de proximo passo. O agente seguinte no handoff le essa pasta automaticamente antes de agir.

3. **Loop de Feedback Humano com Pause Points**
   ALEX define pontos de parada obrigatorios no pipeline de teste (`--pause-on <diff|fail|gate>`). Em cada pause, o terminal exige resumo visual: diff comportamental, probes divergentes e pacote de evidencias. ALEX responde com `continue`, `rollback`, ou `inspect:<id>`. Cada decisao vira regra que alimenta o gate automatizado.

4. **Dashboard Terminal com Watch Mode**
   Comando `quimera test:dashboard` que exibe no terminal: ultimas N execucoes por agente, taxa de aprovacao por tipo de cenario, falhas mais frequentes com contagem, e componentes quentes. Atualiza em tempo real com `--watch`. Filtros por `--agent`, `--scenario-type`, `--status=fail`.

5. **Glossario Compartilhado de Padroes de Falha**
   Arquivo `docs/failure-glossary.md` gerado automaticamente que cataloga padroes de falha recorrentes (`slot-stuck`, `orphan-handoff`, `evidence-leak`, `queue-silence`). Cada entrada tem: nome, descricao, sinal de deteccao, acoes corretivas tipicas, e quais agentes sao mais afetados. ALEX adiciona entradas manualmente com `quimera fail:new`.

### Contribuicoes de NEMOTRON-3-SUPER (Bug investigation)

1. **Perfil de Tool Call como Assinatura de Bug**
   Cada sessao gera uma sequencia de tool calls com latencias, retries e payloads. Quando um bug e reportado, o sistema compara o perfil de tool calls da sessao bugada contra um corpus de sessoes canonicas. O desvio estatistico aponta a causa raiz sem exigir diff de alto nivel.

2. **Bug Magnet -- Mutacao Probabilistica de Sessoes**
   Agente cujo unico trabalho e pegar sessoes validas e aplicar mutacoes controladas: atrasos aleatorios em tool calls, drop de respostas, corrupcao de `evidence_context`, handoffs reordenados, timestamps fora de ordem. Cada mutacao que quebrar o smoke test revela uma suposicao oculta no codigo.

3. **Fuzzing Semantico de Prompt**
   Dada uma sessao real, gerar N variacoes dos prompts do usuario (parafraseio, sinonimos, erros de digitacao, reordenacao de comandos) e executar todas contra o replay. Se o comportamento divergir significativamente para entradas semanticamente equivalentes, ha um bug de robustez.

4. **Reproducao por Execucao Reversa com Checkpoint**
   Quando um assert comportamental falha, o sistema captura o estado interno em cada turno ate o ponto da falha. A partir do checkpoint saudavel mais proximo, o replay e refeito incrementalmente com probes ate encontrar o conjunto minimo de condicoes que disparam o bug.

5. **Contrainsight: O Falso Replay Verde**
   O replay pode passar em todos os asserts mesmo quando o comportamento runtime esta errado -- se os asserts forem muito grossos ou se o oraculo foi gerado a partir de uma sessao ja bugada. E necessario um agente "cetico" dedicado a provar que cada oraculo esta errado, gerando probes adversariais. Sem isso, a suite de regressao pode silenciosamente se tornar um teatro de aprovacao.

### Sintese da segunda rodada

Os quatro agentes convergiram sem combinacao previa em tres temas:

- **Mutacao como ferramenta de descoberta**: RING propoe `MutationEngine`, NEMOTRON propoe `Bug Magnet` e QWEN propoe `fuzzing`. Todos chegaram a ideia de que o sistema de teste precisa gerar variacoes problematicas de sessoes validas para encontrar suposicoes ocultas.

- **Contrato como base de assert automatizado**: RING sugere contratos por tipo de agente, QWEN sugere schema de toolbar e contrato de handoff. Documentacao e assercoes derivadas de contratos formais, nao de cenarios manuais.

- **Observabilidade e teste sao o mesmo sistema**: RING propoe barramento unico, MINIMAX propoe cartoes de cenario e dashboard, QWEN propoe snapshot de toolbar. O dado que serve para ALEX depurar tambem deve servir para o agente validar.

O **contrainsight mais importante** veio de NEMOTRON: o Falso Replay Verde. Se os asserts sao fracos ou o oraculo foi construido a partir de dados ja corrompidos, o replay passa mas o sistema ainda esta quebrado. Ter um agente cetico dedicado a provar que cada oraculo esta errado e a salvaguarda que nenhuma das outras ideias endereca diretamente.

### Contribuicoes de CODEX (Engenharia de codigo e testes)

CODEX produziu uma lista priorizada de 10 acoes com arquivos, linhas e justificativa tecnica. Diferente dos demais agentes que focaram em conceitos arquiteturais, CODEX focou em mudancas incrementais implementaveis na base existente.

**P0 — Implementacao imediata (curto prazo)**

1. **Extrator de evidencia para comportamento real de tool calls (`tool_result` + `assertion`)**
   - Arquivos: `quimera/evidence/parser.py:75-98`, `quimera/evidence/models.py:9-16`
   - O pipeline hoje captura intencao (file read/edit, think), mas nao resultado real de execucao. Um extrator que parseia saidas de `exec_command`, `rg`, `pytest` e extrai assertions pass/fail, exit codes e diffs como `Evidence(type="behavior_assertion")` permite que agentes seguintes validem se mudancas quebraram algo sem reexecutar.
   - Exemplo: `Evidence(type="tool_result", summary="pytest: 12 passed, 0 failed")`

2. **Query incremental no PromptBuilder com `since_ts`**
   - Arquivos: `quimera/evidence/store.py:27-43`, `quimera/prompt.py:207-221`
   - Hoje `_build_evidence_section()` faz query completa da sessao a cada turno. Adicionar `last_evidence_ts` no `shared_state` permite query incremental e formatacao separada de "Evidencias Novas" vs "Historico".

3. **Validacao de digest no formatter com aviso de stale**
   - Arquivos: `quimera/evidence/formatter.py:10-89`
   - `EvidenceStore.is_valid()` ja existe mas o formatter ignora digests. Modificar para, ao listar arquivos visitados, verificar se o digest atual do arquivo ainda corresponde ao registrado. Se nao, incluir `⚠️` no prompt: arquivo modificado desde a leitura.

4. **Campo `confidence` no modelo Evidence**
   - Arquivo: `quimera/evidence/models.py:9-16`
   - Evidencias de tipo `tool_result` ou `behavior_assertion` precisam carregar metadata de confianca. Adicionar `confidence: float` (0.0-1.0). O formatter prioriza evidencias de alta confianca.

5. **Evidencias via `_record_tool_event` em vez de so regex**
   - Arquivos: `quimera/app/core.py:913-944`, `quimera/evidence/__init__.py`
   - `_record_tool_event` ja recebe tool events com `ok`, `error_type`, `reason`. Conectar ao `EvidenceStore.append()` para gerar evidencias automaticas de tipo `tool_call` no momento da execucao, eliminando dependencia exclusiva de regex em stdout.

**P1 — Medio prazo**

6. **Session Replayer**
   - Arquivo novo: `quimera/testing/replayer.py`
   - Componente que le `session.jsonl`, reidrata `shared_state`, `history`, `evidence` e reexecuta comandos. Valida que o output final corresponde ao esperado. Integra com `quimera/runtime/tasks.py`.

7. **Teste end-to-end do pipeline completo com AgentClient mockado**
   - Arquivos: `tests/conftest.py`, `tests/fixtures/`, novo `tests/test_agent_behavior.py`
   - Fixture `full_session` que configura `QuimeraApp` minimo, injeta respostas mock via AgentClient, e verifica que evidencias foram persistidas e o prompt seguinte contem `<evidence_context>`.

8. **Teste de concorrencia para EvidenceStore (race conditions)**
   - Arquivo: `tests/test_evidence_store.py`
   - O store usa `open(..., "a+")` sem lock. Em execucao paralela (threads > 1), multiplos agentes podem escrever concorrentemente. Teste que dispara N threads escrevendo evidencias e verifica integridade do JSONL.

9. **Comando `/evidence assert` para uso humano e CI**
   - Arquivos: `quimera/app/system_layer.py`, `quimera/constants.py`
   - `/evidence assert <type> <path> <expected>` que permite verificar programaticamente se uma evidencia especifica existe no store da sessao. Util para scripts de CI e para o replayer validar passos.

**P2 — Qualidade continua**

10. **Teste de regressao do formatter com corpus real de sessoes**
    - Arquivos: `tests/test_evidence_formatter.py`, `tests/fixtures/evidence/`
    - Fixtures com JSONL reais de sessoes longas (50+ evidencias) que testam: nunca excede `max_chars`, nao perde paths unicos por dedup errado, ordena corretamente por recencia.

**Relacao com as demais contribuicoes:**

As 10 acoes de CODEX sao mais implementaveis no curto prazo que as dos demais agentes, porque:
- Nao exigem novos componentes arquiteturais (probe server, replay system completo)
- Atuam sobre arquivos e interfaces que ja existem e estao testados
- Cada acao pode ser implementada e validada independentemente
- Resolvem os problemas apontados por NEMOTRON nas vulnerabilidades #1 (evidence de stdout vs filesystem), #3 (heuristica fragil de normalize_tool_data), #6 (perda de ordem causal) e #7 (store sem purge)

O Session Replayer (acao #6 de CODEX) pode ser implementado como um consumer do formato de sessao proposto por QWEN, e o comando `/evidence assert` (acao #9) se conecta diretamente aos probes de RING.

## Cuidados para nao piorar

O debate tambem reforcou que varias ideias boas podem dar errado se forem aplicadas sem disciplina.

### Falsas solucoes provaveis

- "Mais logs = mais qualidade".
- "Replay de sessao resolve tudo".
- "Smoke test real substitui teste unitario".
- "Debug bundle completo sempre ajuda".
- "Mais autonomia ja permite agente testar como humano".
- "Tracing estruturado de tool call garante entendimento".

### Riscos concretos

- Instrumentar tudo antes de definir quais sinais mudam decisao.
- Medir comportamento visual sem contrato semantico dos indicadores.
- Criar e2e demais e perder capacidade de localizar causa raiz.
- Tratar pensamento do agente como evidencia operacional confiavel.
- Usar log de sessao como verdade canonica sem validar estado real.
- Automatizar abertura, execucao e merge antes de isolar execucao e rollback.

### Regras de higiene

- Separar fato observado, estado derivado, inferencia do agente e evento de render.
- Priorizar replay com assert semantico, nao similaridade textual bruta.
- Manter debug bundles curados e minimos para decisao.
- Exigir testes em camadas: unidade, integracao e smoke real.
- So aumentar autonomia depois de worktree ou sandbox, gate minimo e rollback simples.
- Tratar observabilidade como produto de decisao, nao como armazenamento bruto.

## Veredito

O Quimera ja tem valor real como orquestrador local multiagente para power users.

Ele ainda nao esta pronto para self-update confiavel.

O proximo salto nao e "mais autonomia". E construir um ciclo em que os agentes consigam observar, reproduzir, validar e recuar com a mesma disciplina que um humano experiente usa ao testar o sistema.
