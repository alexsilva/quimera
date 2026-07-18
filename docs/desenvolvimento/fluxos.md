# Fluxos de execução

## Mensagem normal

```text
input do usuário
  -> ChatProcessor lê linha
  -> SystemLayer verifica comando interno
  -> CommandRouter resolve modo/agente
  -> ChatRound monta prompt e chama AgentGateway/AgentClient
  -> driver CLI ou API produz resposta/stream
  -> parser detecta tool calls ou state updates
  -> ToolLoop executa ferramentas se necessário
  -> renderer mostra resposta
  -> storage salva histórico e métricas
```

## Comando slash

```text
input começa com '/'
  -> aliases são normalizados
  -> SystemLayer tenta comando interno
  -> comandos de modo são tratados pelo CommandRouter
  -> prefixos de agente viram roteamento explícito
  -> comandos desconhecidos podem virar mensagem normal se forem prefixos válidos
```

## `/task`

```text
/task descrição
  -> parse_task_command
  -> classify_task
  -> TaskRouter.choose_agent_with_load_balance
  -> TaskRepository.create_task
  -> executores em background recebem wake()
  -> refresh_task_shared_state
  -> mensagem de sistema exibe id, agente e tipo
```

## Execução de task

```text
executor acorda
  -> claim_task(agent)
  -> monta prompt de task
  -> chama agente
  -> classifica resultado de execução
  -> envia para pending_review ou failed/completed conforme política
  -> publica eventos de domínio
```

## Review de task

```text
task pending_review
  -> política escolhe revisor != executor
  -> revisor avalia resultado
  -> classify_task_review_result
  -> completed se aprovado
  -> pending se precisa retrabalho e há failover
  -> failed se não há recuperação
```

## Tool call via MCP

```text
agente chama tools/call
  -> MCPServer valida JSON-RPC e autenticação
  -> converte argumentos para ToolCall
  -> ToolExecutor aplica política/aprovação
  -> handler executa
  -> resultado volta como content MCP
```

## Delegation entre agentes

```text
agente A chama delegate
  -> ferramenta resolve agente B no pool
  -> dispatch de background cria AgentClient isolado por chamada
     (cancel_event próprio; herda pause_idle_if e process_supervisor
      do client do chat)
  -> prompt de delegação é montado
  -> B executa no client isolado, sem tocar no run() ativo de A
  -> resposta retorna para A como resultado de tool
  -> eventos e evidências podem ser registrados
```

Garantias do fluxo:

- `AgentClient.run()` não é reentrante; delegações nunca reutilizam o client
  principal do chat (execução concorrente sobre o mesmo client é logada como
  erro).
- ESC/Ctrl+C do usuário cancela o agente origem e propaga aos clients de
  background vivos (`AgentClient.add_cancel_listener` →
  `TaskExecutorPool.cancel_background_work`).
- O client isolado herda `pause_idle_if`, então um delegado aguardando uma
  tool longa (ex.: `exec_command` rodando testes) não morre por idle timeout;
  e herda `process_supervisor`, então seus subprocessos entram no
  `terminate_all()` de shutdown/cancelamento.
