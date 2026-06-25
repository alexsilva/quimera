# AGENTS - Detalhes e Capacidades

O Quimera organiza os agentes por **Tiers** e **Especialidades**. Todos os agentes compartilham o mesmo servidor MCP da sessão, que expõe as ferramentas do runtime via protocolo MCP (`2024-11-05`).

O sistema **Cross-MCP** permite que agentes deleguem tarefas entre si através da tool `delegate` — qualquer agente MCP-capaz pode chamar qualquer outro agente do pool, com suporte a fallback e cadeias de delegação (`steps`).

## Tier 3: Orquestradores de Alto Nível

### Gemini (`/gemini`)
- **Foco**: Arquitetura, design de sistemas, refatoração de larga escala.
- **Diferenciais**: Longo contexto, suporte a ferramentas de shell e manipulação de arquivos robusta.
- **Uso Recomendado**: "Redesenhe este sistema de logs para suportar persistência SQLite", "Implemente este novo protocolo entre agentes".
- **MCP**: Sem suporte — não possui driver MCP injetável.

### Claude (`/claude`)
- **Foco**: Arquitetura, revisão de código, documentação e desenvolvimento geral.
- **Diferenciais**: Longo contexto, raciocínio detalhado e alta precisão em tarefas de engenharia.
- **Uso Recomendado**: "Revise este módulo e proponha melhorias", "Documente o protocolo de tasks".
- **MCP**: Suporte completo — injeção via `--mcp-config` JSON.

## Tier 2: Engenheiros de Software

### Codex (`/codex`)
- **Foco**: Geração de código, testes e bug investigation.
- **Diferenciais**: Forte em `code_edit`, `test_execution` e `bug_investigation`.
- **MCP**: Suporte completo — injeção via `-c mcp_servers.quimera.*` argumentos CLI.

## Tier 1: Especialistas OpenCode

A família OpenCode oferece modelos especializados para tarefas menores, otimizando custo e eficiência. Todos os agentes OpenCode têm suporte a MCP — injeção via variável de ambiente `OPENCODE_CONFIG_CONTENT`.

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


## Regra de Input Interativo (termios BANIDO)

- **Proibido** usar `termios`, `tty`, `tty.setraw`, `tty.setcbreak` ou
  `termios.tcsetattr`/`tcgetattr` diretamente em qualquer código do projeto.
  O raw-mode manual a partir de threads de background (ex.: aprovação de
  ferramenta em modo `--threads`) conflita com o terminal gerenciado pelo
  `prompt_toolkit` e **trava o input, o shell e o sistema**.
- Todo input interativo (aprovação de ferramenta, `ask_user`, seleção de
  opções) deve usar **o mesmo input usado para escrever no chat**: leitura por
  linha (cooked mode) via `InputGate`/`prompt_toolkit`. O usuário digita a
  resposta (`y`/`n`/`a`, número da opção ou texto) e confirma com **Enter**.
- A partir de threads de background, leia sempre através dos helpers do
  `InputGate` baseados em `run_in_terminal` (`read_input_in_terminal`,
  `read_selection_in_terminal`, `read_approval_in_terminal`) — eles suspendem o
  prompt e restauram o terminal sem manipular flags de TTY manualmente.
- Não há mais navegação por setas em seleções; isso é intencional. Seleção é
  numerada e confirmada por Enter.
- `TtyController` é no-op por compatibilidade; não reintroduza supressão de eco
  via termios.

## Teste Interativo Local

Quando trabalhar neste projeto e precisar comprovar fluxos interativos sem provedores externos, use o modo de teste explícito. Os profiles fake só devem entrar na rodada com `--test`; sem esse parâmetro, eles não fazem parte do uso humano normal.

Fluxo recomendado para validar chamadas OpenAI-compatible com ferramentas via MCP:

1. Rode o app em modo de teste. O próprio `--test` registra os fake profiles, sobe o backend OpenAI-compatible fake em uma porta livre e aplica override não persistente para o processo:
   ```bash
   python quimera.py --test --agents fake-cli-delegation fake-openai --visibility full
   ```
2. Envie um prompt como `Execute pwd via shell usando o agente OpenAI` e confira no output `MCP conectado`, `MCP tool_call: delegate`, a execução do agente `fake-openai`, a aprovação/execução da ferramenta solicitada por ele e `MCP tool_result: OK`.
3. Use `python -m quimera.devtools.fake_agents openai-server` apenas como ferramenta manual de debug; ela não é pré-requisito para `python quimera.py --test`.

Para testar o driver diretamente, também use `--test`:

```bash
python quimera.py --test --driver-repl fake-openai --prompt "Leia o README usando ferramentas"
```
