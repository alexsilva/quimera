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

`ToolExecutor` recebe `ToolCall`, aplica configuração/política, chama handlers concretos e retorna resultados normalizados. Os schemas usados por MCP e drivers OpenAI-compatible vêm de `runtime/drivers/tool_schemas.py`. A arquitetura de aprovação, escopos temporários, contexto confiável e locks de concorrência está detalhada em [Aprovação e segurança de ferramentas](aprovacao.md).

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

### TerminalRenderer e estilo

O `TerminalRenderer` aplica estilos visuais como `dim`, `bold` e cores ANSI ao output. Recentemente, foram feitas as seguintes mudanças:

1. **build_replace** (renderer.py:401): Adicionou `\033[2m...\033[0m` para aplicar estilo dim a texto transitório.
2. **show_turn_summary** (renderer.py:1611-1613): Passa `muted=True` para `show_feed()` para aplicar estilo dim a resumos de ferramentas.

O efeito dim/muted aparece no output quando terminal suporta SGR 2 (ANSI dim) e o pipeline de estilo não perde o atributo.

### Problemas recentes no estilo

O estilo `dim` foi quebrado após mudanças no pipeline de renderização:

1. O pipeline de estilo (show_plain → writer) pode estar perdendo o atributo dim antes de chegar ao writer
2. Alguns terminais Linux podem ignorar SGR 2 (dim)
3. O estilo `dim` aplicado via Rich Style pode ser sobreposto por Live/TransientOverlay com estilo próprio

Solução: Investigar o pipeline de estilo linha a linha para identificar onde o `dim` está sendo perdido. Fornecer evidência concreta para localização e correção do bug.

### Usuário usando MCP

A interface do usuário usa `AgentGateway` / `AgentClient` para comunicar com o driver OpenAI-compatible subjacente, através de MCP para agentes externos ou streams nativos para execução local. O renderizador aplica themas que podem ser alterados via `UIThemes` ou variáveis de ambiente.
