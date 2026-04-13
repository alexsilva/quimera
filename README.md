# Quimera

Chat multi-agente no terminal que orquestra uma equipe de agentes de IA (**Claude**, **Codex**, **Gemini**, **Qwen** e a família **OpenCode**) para resolver tarefas complexas de engenharia de software. Os agentes colaboram, delegam subtarefas e mantêm um estado compartilhado.

## Como funciona

O Quimera utiliza um protocolo de comunicação entre agentes que permite:
- **Roteamento Inteligente**: As mensagens são direcionadas aos agentes com base em suas especialidades e capacidades.
- **Sistema de Tarefas**: Decomposição de objetivos em tarefas menores via `/task`, que são executadas autonomamente.
- **Estado Compartilhado**: Todos os agentes têm acesso ao contexto do workspace, decisões tomadas e progresso das tarefas.
- **Balanceamento de Carga**: Distribuição automática de tarefas baseada no `effective_score`, combinando competência do agente com penalidade por tarefas abertas.

## Pré-requisitos

Os agentes dependem de suas respectivas CLIs instaladas e autenticadas:
- `claude` (Anthropic)
- `codex` (OpenAI/Codex)
- `gemini` (Google)
- `opencode` (OpenCode family)

## Instalação

```bash
git clone git@github.com:alexsilva/quimera.git
cd quimera
pip install -e .
```

## Uso

Inicie o chat no diretório do seu projeto:

```bash
quimera
```

### Comandos principais

| Comando | Descrição |
|---|---|
| `/task <descrição>` | Cria uma nova tarefa. O Quimera escolherá o melhor agente disponível para executá-la. A execução é exibida em tempo real no terminal. |
| `/claude`, `/codex`, `/gemini`... | Direciona a mensagem especificamente para um agente. |
| `/context` | Exibe ou edita (`/context edit`) o contexto persistente do projeto. |
| `/history` | Exibe o histórico recente da sessão. |
| `/exit` | Encerra a sessão e gera um resumo automático para a próxima execução. |

### Orquestração e Balanceamento

O Quimera avalia cada tarefa e agente usando critérios de:
- **Tier Base**: Cada plugin possui um `base_tier` que representa sua força relativa para roteamento.
- **Especialidade**: O score sobe quando o tipo da tarefa aparece em `preferred_task_types` e cai quando aparece em `avoid_task_types`.
- **Capacidade**: O roteador considera `supports_code_editing`, `supports_long_context` e `supports_tools` conforme o tipo da tarefa.
- **Disponibilidade**: O `effective_score` final é calculado como `base_score - open_tasks`, reduzindo a chance de um único agente monopolizar a fila.
- **Transparência**: O progresso das tarefas é reportado ao vivo para que o usuário acompanhe a "conversa" interna e as ações tomadas.

Na prática, isso permite diferenciar:
- **Gemini / Claude**: Tier base 3, favorecidos em tarefas com mais contexto, revisão e arquitetura.
- **Codex**: Tier base 2, forte em `code_edit`, `test_execution` e `bug_investigation`.
- **Qwen / OpenCode**: Tier base 1, usados principalmente quando a especialidade combina e a carga dos agentes mais fortes está maior.

### Validação de Conclusão

As tarefas são monitoradas por um sistema de sinalização rigoroso:
- **Resposta Obrigatória**: `None`, texto vazio ou resposta sem conteúdo útil após remover blocos de ferramenta são tratados como falha.
- **Blocked Markers**: Identifica incapacidade explícita do agente, com marcadores como "não consigo", "não posso", "unable to", "cannot" e similares.
- **Needs Input**: Detecta quando a intervenção humana é necessária via `[NEEDS_INPUT]`; nesse caso a task não é marcada como concluída.
- **Failover Antes do Fail**: Se a execução falha, a task volta para `pending` e outro agente pode assumi-la; ela só deve parar em `failed` quando não houver rota alternativa viável.

### Isolamento de Execução

Tasks criadas via `/task` são executadas em background sem interferir com o fluxo do chat:
- A resposta do agente **não é adicionada ao histórico principal** da sessão.
- **Sem streaming concorrente** no terminal durante o chat — a saída da task aparece como status dedicado.
- O agente executor recebe apenas o contexto da task, sem o histórico completo da conversa.

### Observabilidade

Além da saída em tempo real no terminal, o runtime mantém métricas básicas por sessão e por agente:
- contagem de handoffs enviados e recebidos
- sucesso ou falha por execução
- latência acumulada
- métricas comportamentais persistidas em estado local para reaproveitar sinais entre sessões

Essa observabilidade hoje é usada principalmente para depuração, análise de comportamento e continuidade do contexto operacional.

### Continuidade Sob Limites de API

Para aumentar o "tempo útil" de sessão dos agentes mesmo quando a API externa impõe limites de contexto, throughput ou disponibilidade, a estratégia mais segura no Quimera é reduzir dependência de contexto bruto e tornar cada rodada retomável:
- usar resumo incremental de sessão como memória canônica, truncando histórico antigo antes de estourar janela de contexto
- persistir estado operacional mínimo em disco (`shared_state`, resumo, histórico recente e tasks) para permitir retomada após falha, timeout ou troca de agente
- decompor trabalho longo em tasks menores com checkpoint explícito, evitando uma única execução grande e frágil
- aplicar failover entre agentes compatíveis quando um backend ficar indisponível ou degradado
- registrar métricas de falha, latência e resposta vazia para detectar quando um provedor externo está reduzindo a qualidade da sessão

Na prática, isso não "remove" o limite da API; isso faz o sistema sobreviver melhor a ele e manter continuidade operacional entre chamadas e entre sessões.

### Ferramentas de Runtime

Quando um agente usa o driver `openai_compat`, o Quimera pode expor ferramentas nativas do runtime para operar direto no workspace:
- `list_files`, `read_file`, `grep_search` para inspeção
- `apply_patch` para alterações parciais e seguras em arquivos existentes
- `write_file` para criação de arquivos e reescrita completa apenas quando isso for explícito
- `run_shell` para comandos permitidos e aprovados

Na prática, `apply_patch` é a primitive preferida para edição, porque evita sobrescrever arquivos inteiros quando só alguns trechos precisam mudar. `write_file` não deve sobrescrever arquivo existente por padrão; para isso, o agente precisa usar `replace_existing=true`.

## Agentes Disponíveis

| Agente | Especialidade Principal | Tier |
|---|---|---|
| **Gemini** | Arquitetura, Refatoração Complexa, Design de Sistemas | 3 |
| **Claude** | Arquitetura, Revisão, Documentação, Codificação Geral | 3 |
| **Codex** | Geração de Código, Testes, Bug Investigation | 2 |
| **OpenCode Family** | Tarefas específicas (Pickle para edição, Omni para review, etc) | 1 |

Veja [AGENTS.md](./AGENTS.md) para detalhes completos de cada plugin.

## Estrutura do Projeto

```
quimera/
  runtime/
    task_planning.py  — lógica de classificação e roteamento
    task_executor.py  — motor de execução autônoma
    tasks.py          — persistência e estado das tarefas
  plugins/            — implementações dos agentes (Claude, Gemini, etc)
  app.py              — orquestrador central e interface
```

## Licença

Uso pessoal e experimental.
