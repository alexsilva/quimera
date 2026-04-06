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
| **Qwen 3.6** | `/opencode-qwen` | Codificação | `code_edit`, `bug_investigation` |

---

## Classificação de Tarefas

O Quimera classifica automaticamente as solicitações enviadas via `/task` nos seguintes tipos:

1. **`code_edit`**: Refatoração, correção, implementação.
2. **`architecture`**: Design de sistemas, novos protocolos.
3. **`code_review`**: Revisão e análise de código.
4. **`bug_investigation`**: Busca pela causa raiz de falhas.
5. **`test_execution`**: Execução e reparo de testes.
6. **`documentation`**: Criação ou atualização de README, MDs e DOCs.

## Lógica de Roteamento (`Effective Score`)

Para cada tarefa, o roteador calcula:
`effective_score = base_score(agente, tarefa) - total_de_tarefas_abertas(agente)`

Isso garante que:
- O agente mais qualificado para a tarefa seja priorizado.
- O trabalho seja distribuído caso o agente principal já tenha muitas tarefas pendentes.
- Agentes especializados (OpenCode) sejam usados para tarefas simples, liberando os orquestradores (Gemini) para arquitetura complexa.
