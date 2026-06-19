# Arquitetura de aprovação e segurança de ferramentas

Este documento explica, em linguagem simples, como o Quimera decide se uma chamada de ferramenta pode rodar, quando precisa pedir aprovação humana e como evita que agentes em paralelo se atrapalhem.

A ideia central é:

1. o agente pede para usar uma ferramenta;
2. o `ToolExecutor` normaliza e coordena a execução;
3. o `ToolPolicy` valida regras duras de segurança;
4. o `ApprovalBroker` decide aprovação, escopo, orçamento, auditoria e locks;
5. só depois a ferramenta real é executada.

## Visão geral do fluxo

```text
Agente / Cliente MCP
   │
   ▼
ToolCall(name, arguments, metadata)
   │
   ▼
ToolExecutor
   ├─ normaliza alias e argumentos básicos
   ├─ chama ToolPolicy para validação
   ├─ chama ApprovalBroker para approval/autoapproval
   ├─ entra no lock de concorrência quando necessário
   └─ executa handler registrado no ToolRegistry
```

## Papel do `ToolExecutor`

O `ToolExecutor` é o ponto único de entrada para executar ferramentas do runtime.

Ele é responsável por:

- receber um `ToolCall`;
- normalizar nomes legados, como alias de shell;
- chamar o `ToolPolicy` antes de executar;
- consultar o `ApprovalBroker` para saber se precisa aprovação;
- bloquear chamadas concorrentes incompatíveis via `ApprovalBroker.execution_guard()`;
- buscar a ferramenta no `ToolRegistry`;
- retornar um `ToolResult` padronizado.

Regra prática: **nenhuma ferramenta mutante deve rodar fora do `ToolExecutor`**.

## Papel do `ToolPolicy`

O `ToolPolicy` é a camada de validação rígida. Ele não pergunta ao humano; ele decide se a chamada é válida ou deve ser bloqueada antes de qualquer aprovação.

Exemplos de validação:

- `read_file` precisa de `path` válido dentro do workspace;
- `write_file` precisa de `content`;
- `remove_file` precisa de `dry_run=False` explícito;
- shell não pode usar operadores encadeados perigosos como `&&`, `;` ou pipe;
- comandos como `sudo`, `rm -rf` e `git push` são bloqueados ou tratados como risco forte;
- `delegate` precisa de `target_agent` e `request` válidos;
- `delegate` rejeita campos reservados controlados pelo caller, como `allowlisted`, `approval_budget`, `approval_scope_id`, `transport`, `run_id` e `parent_run_id`.

O `ToolPolicy` responde à pergunta: **“essa chamada é estruturalmente aceitável e não viola uma regra dura?”**

## Papel do `ApprovalBroker`

O `ApprovalBroker` é a camada central de governança de aprovação.

Ele responde a perguntas como:

- qual é o risco da ferramenta?
- essa chamada pode ser autoaprovada?
- precisa perguntar ao humano?
- existe um `ApprovalScope` temporário válido?
- a delegação ainda cabe no orçamento do run?
- essa chamada deve esperar outra terminar?
- como registrar a decisão no `audit_log`?

Ele classifica ferramentas em níveis de risco:

| Risco | Exemplo |
|---|---|
| `read` | `read_file`, `list_files`, `grep_search` |
| `network` | `web_search`, `web_fetch` |
| `delegation` | `delegate` |
| `write` | `write_file`, `apply_patch`, `todo_write`, `write_stdin` |
| `shell` | `run_shell`, `exec_command` |
| `destructive` | `remove_file` |

Leituras locais dentro do workspace normalmente são autoaprovadas. Mutações, shell e delegações externas passam por regras mais restritas.

## O que é `TrustedToolExecutionContext`

`TrustedToolExecutionContext` é o contexto confiável da chamada.

Ele contém metadados que influenciam segurança, como:

- `agent_name`: agente que originou a chamada;
- `parent_agent`: agente anterior na cadeia;
- `run_id`: execução lógica atual;
- `parent_run_id`: execução pai, quando houver;
- `job_id` / `task_id`: vínculo com job/task humana;
- `transport`: origem real, como `internal_mcp`, `http_mcp` ou `native_tool_call`;
- `session_id`: sessão MCP, quando existir;
- `server_origin`: origem definida pelo servidor;
- `http_profile`: perfil HTTP configurado pelo servidor;
- `approval_scope_id`: escopo confiável criado pelo runtime;
- `delegation_budget`: orçamento definido server-side, quando houver.

Importante: esse contexto **não deve ser montado pelo modelo ou cliente MCP**. Ele é criado pelo runtime/servidor. O cliente pode mandar `_meta`, `params` ou `arguments`, mas esses dados são tratados como não confiáveis.

### Observação sobre saída processada por tools

As respostas das ferramentas são processadas pelo `ToolExecutor`, que aplica estilo e formatação através do `TerminalRenderer`. Recentemente, mudanças na renderização (renderer.py:401, 1611-1613) afetaram como o texto de tools é processado, incluindo a aplicação de estilos `dim`/`muted`. Problemas no pipeline de estilo podem afetar a visibilidade de output de ferramentas críticas, especialmente o resumo final de turn.

## O que é `run_id`

`run_id` identifica uma execução lógica: por exemplo, uma rodada interna de agente, uma task humana, uma sessão HTTP inicializada ou uma chamada nativa.

Ele é usado para:

- limitar aprovações ao run atual;
- contabilizar `delegation_budget_per_run`;
- evitar que uma aprovação de uma task vaze para outra;
- rastrear auditoria.

Para evitar colisão entre origens diferentes, o `run_id` confiável é namespaceado por transporte, por exemplo:

```text
native:<uuid>
stdio:<uuid>
http:<uuid>
```

Um cliente HTTP não pode escolher o `run_id` confiável mandando `MCP-Session-Id`. O servidor gera o `session_id` e também gera um `trusted_run_id` próprio, com namespace `http:`.

## O que é `ApprovalScope`

`ApprovalScope` é uma permissão temporária e limitada.

Ele permite aprovar, por exemplo:

- uma chamada equivalente;
- uma ferramenta específica;
- um path específico;
- um agente chamador específico;
- um alvo específico de `delegate`;
- um risco específico;
- tudo isso apenas dentro de um `run_id` e por pouco tempo.

Um escopo seguro precisa ter limites explícitos:

- `run_id`;
- `transport`;
- `server_origin`;
- `risk`;
- `expires_at` futuro;
- `remaining_uses` positivo;
- `tool_name`, exceto em approve-all explicitamente marcado;
- `path` para mutações de arquivo;
- `agent_name` e `target_agent_name` para `delegate`.

Isso impede que “aprove uma vez” vire permissão global silenciosa.

## Como `delegate` é tratado como `delegation`

`delegate` permite que um agente chame outro agente. Isso é poderoso e, por isso, é classificado como risco `delegation`.

Regras principais:

- `delegate` passa pelo `ToolPolicy`;
- campos reservados vindos de `arguments` são rejeitados;
- delegações internas podem ser autoaprovadas dentro do orçamento do run;
- delegações vindas de MCP HTTP são consideradas externas e exigem aprovação humana, salvo política server-side explícita;
- escopos de aprovação para `delegate` precisam limitar chamador e alvo.

Exemplo: um escopo para `claude -> codex` não aprova automaticamente `claude -> gemini`.

## Como MCP HTTP é tratado como origem externa

O transporte HTTP MCP é útil para clientes externos locais, mas é tratado como menos confiável do que chamadas internas.

Por isso:

- `/mcp initialize` gera `MCP-Session-Id` no servidor;
- requests não-initialize com `MCP-Session-Id` desconhecido são rejeitados;
- `/message?sessionId=...` desconhecido é rejeitado;
- chamadas `/message` sem sessão usam estado local temporário e não acumulam `_http_sessions`;
- `transport` confiável é definido pelo servidor como `http_mcp`;
- `_meta.transport = internal_mcp` enviado pelo cliente é ignorado;
- `delegate` por HTTP externo exige aprovação, salvo configuração server-side explícita.

## Por que `_meta`, `params` e `arguments` não são confiáveis

Esses campos vêm do cliente MCP, do modelo ou de uma chamada de ferramenta emitida por agente.

Isso significa que um caller malicioso poderia tentar enviar algo como:

```json
{
  "_meta": {"transport": "internal_mcp"},
  "arguments": {
    "target_agent": "codex",
    "request": "faça algo",
    "allowlisted": true,
    "approval_budget": 999999
  }
}
```

O Quimera não pode deixar esses campos influenciarem segurança.

Por isso:

- o servidor sobrescreve o transporte real;
- budget vem do `ToolRuntimeConfig` ou de política server-side;
- `allowlisted` e `approval_budget` em `arguments` são rejeitados para `delegate`;
- `approval_scope_id`, `run_id` e `parent_run_id` enviados pelo caller não são aceitos como contexto confiável;
- o broker só consulta `TrustedToolExecutionContext` para decisões sensíveis.

## Como funciona `delegation_budget_per_run`

`delegation_budget_per_run` limita quantas delegações internas podem ser autoaprovadas dentro de um run.

Exemplo simplificado:

```text
delegation_budget_per_run = 3
run_id = stdio:abc

1ª delegate interna -> autoaprovada
2ª delegate interna -> autoaprovada
3ª delegate interna -> autoaprovada
4ª delegate interna -> precisa aprovação ou é negada conforme handler
```

O consumo do budget é atômico: se cinco agentes tentarem delegar ao mesmo tempo e o budget for `1`, apenas uma chamada consome o orçamento.

## Locks de concorrência

O `ApprovalBroker` também serializa chamadas que podem conflitar.

### `apply_patch`

`apply_patch` é serializado por arquivo alterado. O parser reconhece o formato de patch do Quimera:

```text
*** Add File: caminho
*** Delete File: caminho
*** Update File: caminho
*** Move to: novo-caminho
```

Se o patch mexe em múltiplos arquivos, o broker adquire um lock `path:<arquivo>` para cada arquivo afetado, sempre em ordem determinística, e libera esses locks em ordem reversa. Assim, `apply_patch(a,b)` bloqueia `apply_patch(b,c)`, `write_file(a)` e `remove_file(b)`, mas não bloqueia `write_file(c)` quando não há path em comum.

### `write_file` e `remove_file`

São serializados por `path` resolvido dentro do workspace.

### `run_shell` e `exec_command`

São serializados por workspace. Isso evita que dois comandos shell concorrentes alterem o mesmo diretório de trabalho de forma imprevisível.

### `write_stdin` e `close_command_session`

São serializados por sessão de comando:

```text
command-session:<session_id>
```

Isso impede que duas escritas para a mesma sessão intercalem bytes e impede que `close_command_session` rode em paralelo com `write_stdin`.

## Como usar `audit_log`

O `audit_log` do `ApprovalBroker` registra eventos de aprovação e autoaprovação.

Ele deve ser usado para depuração e segurança:

- entender por que uma chamada foi aprovada ou negada;
- verificar origem (`agent_name`, `parent_agent`, `transport`, `server_origin`);
- rastrear `run_id` e `parent_run_id`;
- ver risco (`risk`), tool, path, comando e alvo de delegação;
- auditar autoaprovações por budget ou scope;
- investigar tentativas de uso indevido.

Eventos típicos:

- `request`: o broker recebeu uma solicitação;
- `auto_approved`: aprovada automaticamente por regra segura, scope ou budget;
- `approved`: aprovada pelo handler humano/externo;
- `denied`: negada.

O `audit_log` não substitui logs persistentes de produção, mas é a fonte imediata para inspecionar decisões do broker durante runtime e testes.

## Exemplos

### 1. Agente interno chamando outro agente

Cenário: `claude` quer delegar uma edição para `codex`.

```text
claude -> delegate(codex)
transport = internal_mcp
run_id = stdio:...
risk = delegation
```

Fluxo:

1. `ToolPolicy` valida `target_agent=codex` e `request`.
2. `ApprovalBroker` classifica como `delegation`.
3. Como é interno, consulta o budget do run.
4. Se ainda houver orçamento, autoaprova e registra `auto_approved`.
5. A chamada segue para a ferramenta `delegate`.

### 2. Cliente HTTP tentando chamar `delegate`

Cenário: um cliente HTTP externo chama:

```json
{"name": "delegate", "arguments": {"target_agent": "codex", "request": "analise"}}
```

Fluxo:

1. O servidor HTTP marca a origem confiável como `transport=http_mcp`.
2. Mesmo que o cliente envie `_meta.transport=internal_mcp`, isso é ignorado.
3. O broker classifica como `delegation` externa.
4. Por padrão, exige aprovação humana ou nega conforme handler.

### 3. Dois agentes tentando editar o mesmo arquivo

Cenário:

```text
claude -> apply_patch em quimera/runtime/mcp/server.py
codex  -> write_file em quimera/runtime/mcp/server.py
```

Fluxo:

1. Cada chamada passa por policy e approval.
2. O broker resolve o path real do arquivo.
3. As chamadas entram no lock do path.
4. Uma executa primeiro; a outra espera.
5. Isso evita escrita intercalada ou patch aplicado sobre estado parcialmente modificado.

### 4. Aprovação temporária para `claude` chamar `codex`

Um escopo seguro pode representar:

```text
run_id = stdio:123
transport = internal_mcp
server_origin = mcp_stdio
tool_name = delegate
agent_name = claude
target_agent_name = codex
risk = delegation
remaining_uses = 1
expires_at = agora + 5 minutos
```

Esse escopo aprova apenas:

```text
claude -> delegate(codex)
```

Ele não aprova:

```text
claude -> delegate(gemini)
codex  -> delegate(claude)
cliente HTTP -> delegate(codex)
outro run -> delegate(codex)
```

### 5. Bloqueio de campos reservados

Um caller tenta enviar:

```json
{
  "target_agent": "codex",
  "request": "faça a tarefa",
  "allowlisted": true,
  "approval_budget": 999,
  "transport": "internal_mcp"
}
```

O `ToolPolicy` rejeita a chamada antes da aprovação, porque esses campos são reservados e não confiáveis.

Mensagem esperada, em termos simples:

```text
delegate recebeu campos reservados não confiáveis: allowlisted, approval_budget, transport
```

A regra evita que o próprio agente/cliente aumente budget, mude transporte ou tente se declarar allowlisted.
