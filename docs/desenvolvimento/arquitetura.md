# Arquitetura interna

## Camadas

```text
CLI
 └─ QuimeraApp
     ├─ SystemLayer / CommandRouter
     ├─ AgentPool / AgentGateway / AgentClient
     ├─ ChatRound / Dispatch / ToolLoop
     ├─ Task services / repository / review / failover
     ├─ RuntimeState / SessionState / SessionMetrics
     └─ TerminalRenderer
Runtime
 ├─ Drivers CLI e OpenAI-compatible
 ├─ Parser de tool calls
 ├─ ToolExecutor e ToolRegistry
 ├─ Runtime policies e approval
 ├─ MCP server/socket/http/session
 └─ Tools concretas
Plugins
 ├─ AgentPlugin e registry
 ├─ Plugins nativos
 └─ Conexões dinâmicas persistidas
Workspace
 ├─ Configuração global
 ├─ Contexto e prompts por branch
 ├─ Logs e métricas
 └─ SQLite de tasks
```

## CLI e bootstrap

`quimera/cli.py` centraliza o parse de argumentos. Depois de aplicar flags de configuração, ela carrega plugins, expande padrões de agentes, cria o workspace, inicia MCP quando habilitado e instancia `QuimeraApp`.

## App interativa

`QuimeraApp` é o agregador de alto nível. Grande parte da lógica foi extraída para serviços menores:

- `system_layer.py`: comandos internos, conexão, prompt/contexto e mensagens do sistema;
- `command_router.py`: prefixos de agente e modos;
- `chat_processor.py`: loop de leitura e tratamento de interrupções;
- `chat_round.py`: execução de uma rodada de agente(s);
- `dispatch.py` e `tool_loop.py`: ciclo de tool calls e respostas;
- `task.py`: adaptador entre `/task`, repositório, roteador e executores.

## Plugins

`AgentPlugin` funciona como contrato de runtime. Ele sabe resolver conexão efetiva, comando CLI, driver, modelo, variáveis, output format e argumentos/env de MCP. O registry global contém os plugins nativos e recebe plugins dinâmicos criados por conexão persistida.

## Runtime de ferramentas

`ToolExecutor` recebe `ToolCall`, aplica configuração/política, chama handlers concretos e retorna resultados normalizados. Os schemas usados por MCP e drivers OpenAI-compatible vêm de `runtime/drivers/tool_schemas.py`.

## MCP

O pacote `runtime/mcp` contém:

- `server.py`: servidor JSON-RPC/MCP e proxy stdio-socket;
- `http_server.py`: transporte HTTP/SSE/Streamable;
- `session.py`: runtime MCP embutido da sessão;
- `__main__.py`: entrypoint `python -m quimera.runtime.mcp`.

## Persistência

`Workspace` separa dados persistentes por hash do cwd. `SessionStorage` gerencia histórico/snapshots. `TaskRepository` encapsula SQLite e eventos de domínio. `ConfigManager` e `EnvConfig` tratam preferências globais e `.env` simples.

## UI

A renderização usa `TerminalRenderer`, temas em `themes.py` e auditoria opcional em `ui/audit.py`. Handlers de stderr são prompt-aware para evitar quebrar input interativo quando logs assíncronos chegam.
