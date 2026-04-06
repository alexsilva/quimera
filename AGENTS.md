# AGENTS - Detalhes e Capacidades

O Quimera organiza os agentes por **Tiers** e **Especialidades**.

## Tier 3: Orquestradores de Alto Nível

### Gemini (`/gemini`)
- **Foco**: Arquitetura, design de sistemas, refatoração de larga escala.
- **Diferenciais**: Longo contexto, suporte a ferramentas de shell e manipulação de arquivos robusta.
- **Uso Recomendado**: "Redesenhe este sistema de logs para suportar persistência SQLite", "Implemente este novo protocolo entre agentes".

### Claude (`/claude`)
- **Foco**: Arquitetura, revisão de código, documentação e desenvolvimento geral.
- **Diferenciais**: Longo contexto, raciocínio detalhado e alta precisão em tarefas de engenharia.
- **Uso Recomendado**: "Revise este módulo e proponha melhorias", "Documente o protocolo de tasks".

## Tier 2: Engenheiros de Software

### Codex (`/codex`)
- **Foco**: Geração de código, testes e bug investigation.
- **Diferenciais**: Forte em `code_edit`, `test_execution` e `bug_investigation`.

## Tier 1: Especialistas OpenCode

A família OpenCode oferece modelos especializados para tarefas menores, otimizando custo e eficiência.

| Agente | Prefixo | Especialidade | Preferência |
|---|---|---|---|
| **Big Pickle** | `/opencode-pickle` | Edição de Código | `code_edit` |
| **GPT-5 Nano** | `/opencode-gpt` | Documentação | `documentation` |
| **Mimo Omni** | `/opencode-mimo-omni`| Revisão | `code_review` |
| **Omni Pro** | `/opencode-omni-pro` | Arquitetura | `architecture` |
| **MiniMax** | `/opencode-minimax` | Documentação | `documentation` |
| **Nemotron** | `/opencode-nemotron`| Bugs | `bug_investigation` |
| **Qwen 3.6** | `/opencode-qwen` | Codificação | `code_edit`, `code_review` |

---

## Classificação de Tarefas

O Quimera classifica automaticamente as solicitações enviadas via `/task` nos seguintes tipos:

1. **`code_edit`**: Refatoração, correção, implementação.
2. **`architecture`**: Design de sistemas, novos protocolos.
3. **`code_review`**: Revisão e análise de código.
4. **`bug_investigation`**: Busca pela causa raiz de falhas.
5. **`test_execution`**: Execução e reparo de testes.
6. **`documentation`**: Criação ou atualização de README, MDs e DOCs.

## Regra de Review

- Todo agente com capacidade de **editar código** também é elegível para **`code_review`**.
- Agentes especializados em review continuam tendo prioridade natural quando o score base for maior.

## Regra de Execução

- Capacidade de **editar código** não implica capacidade de **executar código**.
- Agentes com `supports_tools=False` podem gerar patches, analisar código e revisar, mas não devem ser tratados como executores de shell/testes.
- No caso do Qwen, isso significa: elegível para `code_edit` e `code_review`, mas não para `test_execution`.

- Penalidade de Bug Investigation para agentes sem tooling
- Agentes sem tooling (sem `tools`) recebem uma penalidade fixa de -3 no score efetivo ao receber tarefas de `bug_investigation`.
- Agentes com tooling geralmente não recebem essa penalidade para `bug_investigation`; penalidades adicionais podem existir apenas se políticas futuras forem ativadas.
- A penalidade aplica-se apenas a tarefas de `bug_investigation`; `code_edit` e `code_review` não são impactadas por essa regra.
- Exemplo: se um agente A tem base_score 8 e não possui tools, seu score efetivo para `bug_investigation` fica 5 (8 - 3). Já um agente B com tooling tem base_score 7, score efetivo = 7 (sem penalidade). Assim, o agente com tooling pode ser priorizado conforme o cenário.

## Lógica de Roteamento (`Effective Score`)

Para cada tarefa, o roteador calcula:
`effective_score = base_score(agente, tarefa) - total_de_tarefas_abertas(agente)`

Isso garante que:
- O agente mais qualificado para a tarefa seja priorizado.
- O trabalho seja distribuído caso o agente principal já tenha muitas tarefas pendentes.
- Agentes especializados (OpenCode) sejam usados para tarefas simples, liberando os orquestradores (Gemini) para arquitetura complexa.
