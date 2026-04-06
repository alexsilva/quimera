# Quimera

Chat multi-agente no terminal que orquestra uma equipe de agentes de IA (**Claude**, **Codex**, **Gemini**, **Qwen** e a família **OpenCode**) para resolver tarefas complexas de engenharia de software. Os agentes colaboram, delegam subtarefas e mantêm um estado compartilhado.

## Como funciona

O Quimera utiliza um protocolo de comunicação entre agentes que permite:
- **Roteamento Inteligente**: As mensagens são direcionadas aos agentes com base em suas especialidades e capacidades.
- **Sistema de Tarefas**: Decomposição de objetivos em tarefas menores via `/task`, que são executadas autonomamente.
- **Estado Compartilhado**: Todos os agentes têm acesso ao contexto do workspace, decisões tomadas e progresso das tarefas.
- **Balanceamento de Carga**: Distribuição automática de tarefas baseada no `effective_score` (especialidade - carga atual).

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
- **Especialidade**: Algum agente é preferencial para `architecture`, `code_edit`, `documentation`, etc?
- **Capacidade**: O agente suporta edição de arquivos (`supports_code_editing`) ou ferramentas externas?
- **Disponibilidade**: O `effective_score` garante que nenhum agente fique sobrecarregado enquanto outros estão ociosos.
- **Transparência**: O progresso das tarefas é reportado ao vivo para que o usuário acompanhe a "conversa" interna e as ações tomadas.

### Validação de Conclusão

As tarefas são monitoradas por um sistema de sinalização rigoroso:
- **Blocked Markers**: Identifica se o agente reportou incapacidade de realizar a tarefa (ex: "não consigo", "cannot").
- **Needs Input**: Detecta quando a intervenção humana é necessária via `[NEEDS_INPUT]`.
- **Análise de Resposta**: Respostas vazias ou que não resultam em ações concretas quando esperado são classificadas automaticamente como `failed`.

## Agentes Disponíveis

| Agente | Especialidade Principal | Tier |
|---|---|---|
| **Gemini** | Arquitetura, Refatoração Complexa, Design de Sistemas | 3 |
| **Claude / Codex** | Codificação Geral, Review, Testes | 2 |
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
