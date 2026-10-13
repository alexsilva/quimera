# Otimização da TUI sem alterar a interface

## Baseline e escopo

Em 2026-10-12, antes de qualquer alteração, a suíte integral de `e34f69c`
concluiu com exit code 0: **4.157 testes passando e 11 skips** (4.168 coletados).
Não foram atualizadas dependências nem alterados protocolos/configurações de
agentes, limites de histórico, estilos, dimensões, scroll ou atalhos.

A captura real da sessão tinha terminal de 56×48, feed com 43 linhas,
31 slots e altura virtual de 504 linhas. Os logs confirmaram também mudanças
de tamanho causadas pelo teclado virtual. Esse é um baseline de diagnóstico,
não uma medida de desempenho do código novo: o processo ativo só carrega as
mudanças depois de reiniciar.

## Caminhos otimizados

- **Submissões ativas:** o pulso consulta um índice pequeno, em vez de varrer
  todas as mensagens antigas a cada 250 ms. Atualizações de status removem
  entradas terminais; inserções/remoções de slots reindexam as posições.
  O snapshot público e sua ordenação continuam iguais.
- **Cauda de raciocínio:** documentos grandes com quebras LF usam `count` e
  `rfind` e alocam apenas a cauda limitada. Não se cria mais uma lista com
  todas as linhas do texto acumulado em cada render. Documentos pequenos,
  CRLF e os demais separadores de `splitlines` mantêm o algoritmo anterior.
  O limite visual de oito linhas/1.600 caracteres e o rótulo de linhas
  anteriores não mudaram; a resposta final continua integral.
- **Classes do feed:** slots cuja classe `-compact` não mudou deixam de
  chamar o validador CSS. Mudanças da classe continuam funcionando mesmo
  quando o token do evento é o mesmo.
- **Status invisíveis:** os dois widgets de status são escondidos/esvaziados
  uma única vez, em vez de chamar `Static.update("")` em cada poll de 200 ms.
  Isso evita solicitar layout da árvore inteira sem mudança visual.
- **Pulso em idle:** o timer de pensamento/submissão pausa quando não há
  nada para animar e retoma ao reconciliar o feed. O intervalo de 250 ms
  durante atividade e a expiração dos previews MCP continuam funcionando.
  O drain do bridge de 50 ms e o relógio não foram alterados; portanto esta
  rodada não promete eliminar todos os wakeups da aplicação em idle.

## Benchmark reproduzível

```sh
.venv/bin/python -m quimera.devtools.benchmark_textual
```

O script compara os algoritmos anteriores com os atuais **no mesmo processo e
com os mesmos dados**, após verificar equivalência dos resultados. Usa medianas
de cinco repetições de 100 chamadas e mede alocação temporária com `tracemalloc`.
Não abre um terminal e não usa agentes/rede reais.

Uma nova execução na retomada desta sessão produziu:

| Caminho | Carga | Antes | Depois |
|---|---|---:|---:|
| Consultar submissões ativas | 10.001 turnos, um ativo | 3,490482 ms | 0,000337 ms |
| Extrair cauda viva | 10.000 linhas, 1.039.999 caracteres | 1,223424 ms | 0,700164 ms |
| Reconciliar slots estáveis | 1.000 slots | 1,703882 ms | 0,851224 ms |

Na extração da cauda, a alocação temporária de pico caiu de **1.686.193 para
1.237 bytes**. Os ganhos são locais a esses caminhos: não significam a mesma
redução de RSS/CPU total da IDE, nem afetam a latência do provedor de IA.
Tempos dependem da carga, temperatura e concorrência do host.

No app headless real, a janela ociosa após um turno terminou com **zero
escritas nos widgets de status, zero pedidos de layout e zero ticks de pulso**
em um segundo. A primeira entrada em idle ainda tem uma escrita/hide legítima
por widget e um pulso que detecta a ausência de trabalho. Com transient ativo,
o mesmo benchmark continuou registrando quatro pulsos por segundo. O teste
de idle começa depois de assentar esse primeiro layout, não durante o boot.

## Regressões e revisão

`tests/test_textual_performance.py` cobre equivalência da cauda em separadores
Unicode/CRLF, linhas enormes, limites de truncamento e documentos aleatórios;
garante ausência de varredura por pulso; valida revisões terminais, boot tardio,
delegações, reset, clear, restauração e mudanças de classe com token igual.

Revisão independente do modelo/cauda/classes: **claude-sonnet, task 113**,
aprovada, com 49 testes novos passando e 3.000 documentos de fuzz sem divergência.
O bloco TUI, ferramenta de screenshot, wiring debug e regressões de performance
também concluiu com **353 testes passando**.
Após a refatoração do app/timer, os **284 testes de `test_textual_ui.py`**
também passaram. Uma asserção de estrutura desse arquivo foi adaptada para
validar a consulta compartilhada pelo helper de status, sem remover o teste.

`tests/test_textual_idle_performance.py` verifica contagens determinísticas,
pausa idempotente, retomada por lote/worker, esferas de submissões, três estados
terminais, expiry MCP e lifecycle do timer. Os dois arquivos de regressão novos
concluíram com **62 testes passando**, inclusive com `RuntimeWarning` como erro.
A fixture encerra seu worker antes de fechar o loop headless.

Verificação visual independente: **claude-sonnet, task 114**, comparou o pacote
baseline extraído do Git com o working em processos novos. Com o app/timer novo
foram **104 comparações pareadas de texto, geometria e SVG idênticos**, em
56×22 e 56×48, incluindo scroll, resizes, boot, clear, delegações e transients
CLI/API/MCP. O SVG teve somente seu identificador determinístico normalizado;
relógio, cursor e assentamento do frame foram controlados. Também foram
verificados 300.000 passos de fuzz do modelo, 6.000 textos e 12 fases de timers
reais, sem divergência de saída. Artefatos de diagnóstico ficam em `/tmp/vv`.

Revisão final integrada: **claude-sonnet, task 115, APPROVE**. Fable colaborou
no diagnóstico e nas mudanças de idle; sua execução encerrou por quota de uso,
por isso a validação final e os ajustes de testes ficaram com o executor principal.

Na retomada, **codexcloud-gpt-5-6, task 117, APPROVE**, refez a comparação
dos 104 pares de SVG sem divergências e confirmou igualdade byte a byte dos
300.000 passos de fuzz. Também conferiu diretamente a API de `Timer.reset`,
`pause`, `has_class` e `set_class` na fonte do Textual **0.86.0**, mínimo
declarado, e no 8.2.8 instalado, sem atualizar dependências.

Há uma nuance de temporização, não bloqueante: ao pausar pode restar um único
sleep/wakeup já agendado. Se a retomada ocorrer nessa janela, o primeiro pulso
pode chegar entre aproximadamente 250 e 500 ms após o reset, não necessariamente
em 250 ms exatos. O evento novo é renderizado imediatamente por `_sync_feed`;
os pulsos seguintes mantêm o intervalo de 250 ms. Não houve diferença visual
nos artefatos comparados nem perda de expiração MCP ou atualização de submissão.

## Estabilização de testes assíncronos

A validação pós-patch revelou duas races de testes preexistentes, sem mudanças
em executor, locks, delegações, protocolos ou banco de dados de produção:

- `test_runtime_approval_broker.py`: a observação de concorrência numa janela de
  `sleep(0.05)` foi substituída por um rendezvous `Barrier(2)` nos dois handlers.
  **Sonnet, task 116, APPROVE**: três positivos e contraprova negativa falhando
  quando os paths disjuntos recebem a mesma chave de serialização.
- `test_delegate_tracking.py`: observar a task como `cancelled` não significa
  que o job já foi persistido, pois o finalize faz duas escritas separadas. O
  teste agora aguarda o job exato por ID com o helper existente
  `_wait_job_status`, mantendo todas as asserções e o timeout de cinco segundos.
  **Sonnet, task 118, APPROVE**: cinco testes de cancelamento passaram em três
  execuções; ignorar a escrita de cancelamento do job num plugin temporário
  fez o teste falhar por timeout, confirmando que não mascara um job preso.

Os logs das tentativas com falha foram preservados. Nenhum teste foi removido,
marcado como skip/xfail, e nenhum resultado falho foi contabilizado como aprovação.

## Validação integral concluída na retomada

**4.230 testes coletados e contabilizados: 4.219 passed, 11 skipped, zero
failures/errors**, executados em sete blocos sequenciais sobre os mesmos
arquivos congelados. Todos os sete blocos terminaram com exit code 0. A soma
dos XMLs foi conferida contra a coleta: 4.230 testcases únicos, sem duplicação
ou arquivos omitidos. Os 27 hashes SHA-256 das fontes da TUI, testes alterados
e benchmark permaneceram idênticos durante a execução. Não houve warnings nos
logs dos blocos aprovados.

| Bloco | Passaram | Skips | Exit code |
|---|---:|---:|---:|
| 1 | 633 | 0 | 0 |
| 2 | 637 | 0 | 0 |
| 3 | 644 | 2 | 0 |
| 4 | 649 | 0 | 0 |
| 5 | 625 | 9 | 0 |
| 6 | 603 | 0 | 0 |
| 7 | 428 | 0 | 0 |

Os mesmos onze skips ambientais da baseline ficaram explícitos:

- 2 testes OAuth sem a dependência opcional `cryptography`;
- 3 integrações de navegador sem Playwright ou Chrome/Chromium;
- 6 integrações de sandbox sem `bwrap`/suporte a user namespace.

Uma tentativa do bloco 5 usou um `--basetemp` comprido demais para um socket
AF_UNIX, causando `OSError: AF_UNIX path too long` e timeout de inicialização.
Foi corrigido **somente o caminho temporário da execução**, sem editar o teste
nem o servidor MCP. O bloco inteiro foi reexecutado com `/tmp/q385r5` e passou;
os blocos 6 e 7 usaram caminhos igualmente curtos. Os quatro blocos já aprovados
foram preservados após conferir que as fontes continuavam idênticas.

Evidências externas ao repositório:

- `/tmp/quimera-opt-retomada-385/final-v2/`: manifest, comandos de cada bloco,
  logs, XMLs, hashes, progress e `final.json`;
- a tentativa com socket longo permanece nos arquivos `.failed-long-path` e
  em `progress-before-short-basetemp.json`;
- `/tmp/quimera-opt-retomada-385/block-2.log`: diagnóstico da race de cancelamento;
- `/tmp/quimera-opt-retomada-385/benchmark.json`: nova execução do benchmark;
- `/tmp/vv/`: comparação visual e fuzz independentes.

Na retomada anterior (job 385), o processo interativo não foi reiniciado e
não houve commit.
O próximo passo operacional é carregar as mudanças numa nova execução autorizada
e confirmar o comportamento live, sem atribuir ao processo antigo o ganho
medido nos microbenchmarks ou nos testes headless do código novo.

Os arquivos alterados passam no Ruff. O lint global já tinha nove alertas em
arquivos não relacionados antes desta rodada; eles não foram tratados como
regressões nem corrigidos incidentalmente. Para executar pytest neste host,
um `--basetemp` próprio **e curto** evita a limpeza dispendiosa de lixo de sessões antigas
em `/tmp/pytest-of-userland` sem pular testes.

## Validação live da sessão de 2026-10-13 (job 394)

Esta rodada observou a TUI ativa, sem reiniciá-la nem injetar comandos no chat.
As capturas reais começaram em **56×22** e depois mostraram **56×48**; os logs
registraram os resizes do terminal durante o uso. Scrollback, input, previews
e delegações continuaram visíveis. Não houve erro de aplicação nos logs
examinados durante a observação inicial.

O processo atual, PID 2605, iniciou aproximadamente às **00:10:55**, calculado
com os ticks de `/proc` e `CLOCK_BOOTTIME`; o log desta sessão começa às
00:11:14. As quatro fontes modificadas da TUI têm mtimes anteriores, sendo
a edição mais recente às 22:17:40 do dia anterior. Os 27 hashes da validação
anterior continuam idênticos. Isso é evidência de uma inicialização posterior
ao patch, não uma inspeção da identidade dos módulos na memória do processo.
A ressalva sobre o processo antigo acima descreve o job 385, não esta sessão.

**Sonnet, task 119, APPROVE**, repetiu a comparação em processos novos,
usando cópias isoladas do Git e do working tree: **52 pares idênticos** de
texto, geometria e SVG, 26 em cada tamanho (56×22 e 56×48). Os SVGs desta
rodada foram comparados byte a byte. Boot, scroll no topo/meio/fim, resizes,
submissões, transients CLI/API/MCP, delegação e clear foram exercitados.
As 104 comparações e revisões independentes anteriores permanecem preservadas;
os 52 pares novos não são apresentados como uma repetição de todos os temas.

O harness com timers reais também preservou o texto nas **12 fases**. A
reexecução com encerramento do worker antes de fechar o loop passou com
`RuntimeWarning` tratado como erro, sem warnings ou traceback. Em idle
assentado, o pulso ficou pausado, sem avanços ou resets adicionais; atividade
e expiry MCP retomaram o timer. Contagens em janelas reais podem variar por
jitter e pela nuance de retomada já documentada. Esses testes são headless:
não substituem uma medição de idle do terminal vivo.

O benchmark existente foi reexecutado, com os mesmos parâmetros padrão:

| Caminho | Antes | Depois |
|---|---:|---:|
| Consultar submissões ativas | 3,333790 ms | 0,000347 ms |
| Extrair cauda viva | 1,280049 ms | 0,731539 ms |
| Reconciliar slots estáveis | 1,689067 ms | 0,806252 ms |

O pico de **1.237 bytes** continua correto para `_live_tail` **isolada**.
**Codex GPT 5.6, task 120, APPROVE**, destacou um limite importante: a
construção do corpo vivo chama `.strip()` antes dessa função. Com cerca de
1,04 milhão de caracteres, o benchmark adicional do construtor mediu
**3.266 bytes** sem whitespace nas bordas e **1.041.340 bytes** com LF final,
pois o trim copia o texto inteiro. Essa medição ainda não inclui o compositor
ou todos os widgets e não representa RSS total. Portanto o pico baixo da
cauda não deve ser extrapolado para todo o caminho de renderização.

A revisão adicional e sua reprodução mediram `_sync_feed` com cache aquecido
e widgets estáveis, sem compositor: cerca de **0,039 ms para 48 slots**,
**17,54 ms para 10.000**
e **108,68 ms para 50.000**. Existe uma oportunidade em históricos extremos,
mas não um gargalo demonstrado na sessão observada, com dezenas de slots.
As duas revisões recomendaram **não alterar código funcional agora**. Uma
próxima rodada deve medir a carga real antes de propor reconciliação
incremental ou otimização do trim, preservando scrollback e a mesma UI.

A suíte integral foi novamente concluída em sete blocos com exit code 0:
**4.230 testes únicos contabilizados, 4.219 passed, 11 skipped, zero falhas
ou erros**. A coleta nova e a união dos IDs dos blocos são iguais, sem
duplicação ou omissão; **396 hashes** de fontes e testes ficaram inalterados.
Os skips continuam sendo 2 por `cryptography`, 3 por navegador/Playwright e
6 por `bwrap`/user namespace. Não houve warnings nos logs aprovados.

**Sonnet, task 121, APPROVE**, auditou independentemente a coleta, IDs dos
blocos, XMLs, contagens, logs, hashes e limites das afirmações deste documento.
Seu único apontamento foi a falta de artefato para a primeira medida de 48
slots; o ponto foi reproduzido e persistido em `sync-feed-48.json`, e o valor
acima corresponde a essa reprodução, não a uma interpolação.

Uma primeira tentativa falhou por o próprio harness acrescentar `/tmp/q394`
ao `PYTHONPATH`, alterando um valor que um teste de agentes compara. Foi
corrigido somente o carregamento do plugin de coleta e o bloco inteiro foi
reexecutado com sucesso. Um erro posterior de variável no runner ocorreu
antes de lançar o bloco 2; o bloco 1 aprovado foi preservado após conferir
todos os hashes. Nenhum teste ou fonte foi editado para acomodar o harness,
e a tentativa falha não foi contada como aprovação.

Evidências desta rodada:

- `/tmp/q394/`: coleta, comandos, XMLs/logs por bloco, `progress.json`,
  `final.json`, `source-hashes.json`, `runner-attempts.json`, benchmark,
  observação live e comparação visual conferida pelo executor;
- `/tmp/q394sonnet/out/`: 52 pares de SVG e snapshots independentes;
- `/tmp/q394codex/`: script e resultados dos microbenchmarks da revisão;
- `/tmp/q394/clean-timers-{base,work}.{json,log}`: timers com teardown limpo;
- `/tmp/q394/live-body-allocation.json` e `benchmark_live_body.py`:
  medição adicional do trim e script reproduzível;
- `/tmp/q394/sync-feed-48.json` e `benchmark_sync_48.py`:
  reprodução do custo de dezenas de slots;
- screenshots da sessão: `job394-live-*.svg` no diretório de render.

O único arquivo do workspace editado nesta rodada foi este documento. As
fontes funcionais, testes e benchmark anteriores foram mantidos. Não houve
commit, atualização de dependências ou mudança de configuração de agentes.

Foi preparado um observador passivo em `/tmp/q394/observe_idle.py`: aguarda
submissões terminais e 20 segundos de assentamento, depois amostra `/proc`
por 30 segundos. O resultado será `/tmp/q394/live-idle.json`; enquanto o turno
atual estiver ativo, a coleta não representa idle e não é contabilizada como
validação concluída. Uma nova submissão interrompe a amostra. A finalidade
é orientar a próxima decisão de otimização sem reiniciar o app.

## Retomada e preparação de commit (job 398)

A coleta passiva acima terminou às **00:35:37 de 2026-10-13**, no processo
anterior **PID 2605**. Seu resultado foi recuperado nesta retomada:

| Observação | Resultado |
|---|---:|
| Duração real / amostras | 34,622 s / 31 |
| CPU do processo | 15,799% de um CPU lógico (100% = um CPU ocupado) |
| RSS mínimo / máximo | 111.216 / 111.440 KiB |
| RSS inicial / final | 111.340 / 111.380 KiB |
| Threads inicial / final | 26 / 26 |
| FDs inicial / final; mínimo / máximo | 13 / 13; 13 / 21 |

O observador inferiu idle de submissões terminais e 20 segundos de
assentamento; não inspecionou todos os transients, tarefas ou stacks. Portanto
essa janela curta não prova ausência de vazamento, não demonstra melhoria
antes/depois e não mede a memória total da IDE. O RSS variou 224 KiB entre
extremos, com delta inicial/final de +40 KiB. Os FDs tiveram variação
transitória, apesar de terminar no mesmo valor. O uso de CPU remanescente
justifica um perfilamento por stack antes de outra otimização; a amostragem
por TID isolada não identifica a função responsável nem permite atribuí-lo
inteiramente à TUI. O modo debug estava ativo.

A sessão atual, `sessao-2026-10-13-003806`, é outro processo, **PID 12623**,
com início aproximado às **00:37:56**. As quatro fontes funcionais da TUI
continuam com mtimes anteriores à inicialização, sem inspeção da identidade
dos módulos em memória. Não houve reinício ou injeção no app por este job.
Capturas reais `gpt6-retomada-live.svg` e `job398-live-revisao.svg` mostram
feed, input, scrollback e delegação em 56×48; os logs também registram
resizes entre 56×22 e 56×48. A leitura inicial do log desta sessão não
encontrou ERROR/CRITICAL ou traceback. A coleta de idle anterior não é
apresentada como medição desta nova sessão.

A auditoria dos artefatos da suíte integral reconfirmou **4.230 IDs únicos
iguais à coleta, 4.219 passed, 11 skips ambientais, zero falhas/erros e sete
blocos com exit code 0**. Os **396 hashes atuais** de fontes e testes são
idênticos aos da execução do job 394. Não se trata de uma terceira execução
integral: são os resultados anteriores reaproveitados somente após conferir
o congelamento das fontes. As revisões 113–121 e suas evidências permanecem
preservadas.

Uma execução nova dos cinco arquivos de testes relacionados ao candidato
(`test_textual_ui`, `test_textual_performance`, `test_textual_idle_performance`,
`test_runtime_approval_broker` e `test_delegate_tracking`) concluiu com
**430 passed em 45,09 s, exit code 0, sem warnings reportados**. Seu XML é
`/tmp/q398/focused.xml`; esses testes já fazem parte dos 4.230 da suíte e não
são somados novamente à contagem integral.

**Sonnet, task 122, APPROVE**, revisou o diff funcional/testes, conferiu os
396 hashes atuais e executou independentemente os dois arquivos de
performance: **62 passed em 6,76 s**. A revisão registrou os dez SHA-256 das
fontes/testes/benchmark candidatos, sem mudanças nesses arquivos, e confirmou
os limites da observação do processo anterior. A análise reafirmou a ausência
de mudanças de estilo, cor e retenção do scrollback; as evidências visuais
pareadas das revisões anteriores continuam sendo a prova de equivalência.

O benchmark reproduzível padrão foi novamente executado com exit code 0:

| Caminho | Antes | Depois |
|---|---:|---:|
| Consultar submissões ativas | 3,118298 ms | 0,000289 ms |
| Extrair cauda viva | 1,068679 ms | 0,661768 ms |
| Reconciliar slots estáveis | 1,738500 ms | 0,889263 ms |

O pico temporário da cauda isolada permanece **1.686.193 → 1.237 bytes**,
com as ressalvas do `.strip()` e do escopo local já descritas acima. Ruff nos
arquivos Python candidatos e `git diff --check` passaram. Nenhuma nova
otimização funcional foi introduzida nesta retomada sem um gargalo localizado.

Artefatos desta conferência ficam em `/tmp/q398/`: `revalidated-suite.json`,
`previous-idle-summary.json`, `current-live-log-check.json`, `benchmark.json`
e hashes do candidato. O documento é o único arquivo editado por este job;
o candidato de commit inclui também as fontes, testes e benchmark produzidos
nas rodadas anteriores. A aprovação independente do candidato final é
obrigatória antes do commit.
