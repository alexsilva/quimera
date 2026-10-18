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
     └─ Renderer (TextualRenderer na TUI; PlainTextRenderer em pipe)
Runtime
 ├─ Drivers CLI e OpenAI-compatible
 ├─ Parser de tool calls
 ├─ ToolExecutor e ToolRegistry
 ├─ Runtime policies e approval
 ├─ MCP server/socket/http/session
 └─ Tools concretas
Profiles
 ├─ ExecutionProfile e registry
 ├─ Profiles nativos
 └─ Conexões dinâmicas persistidas
Workspace
 ├─ Configuração global
 ├─ Contexto e prompts por branch
 ├─ Logs e métricas
 └─ SQLite de tasks
```

## CLI e bootstrap

`quimera/cli.py` centraliza o parse de argumentos. Depois de aplicar flags de configuração, ela carrega profiles, expande padrões de agentes, cria o workspace, inicia MCP quando habilitado e instancia `QuimeraApp`.

## App interativa

`QuimeraApp` é o agregador de alto nível. Grande parte da lógica foi extraída para serviços menores:

- `system_layer.py`: comandos internos, conexão, prompt/contexto e mensagens do sistema;
- `command_router.py`: prefixos de agente e modos;
- `chat_processor.py`: loop de leitura e tratamento de interrupções;
- `chat_round.py`: execução de uma rodada de agente(s);
- `dispatch.py` e `tool_loop.py`: ciclo de tool calls e respostas;
- `task.py`: adaptador entre `/task`, repositório, roteador e executores.

## Profiles

`ExecutionProfile` funciona como contrato de runtime. Ele sabe resolver conexão efetiva, comando CLI, driver, modelo, variáveis, output format e argumentos/env de MCP. O registry global contém os profiles nativos e recebe profiles dinâmicos criados por conexão persistida.

## Runtime de ferramentas

`ToolExecutor` recebe `ToolCall`, aplica configuração/política, chama handlers concretos e retorna resultados normalizados. Os schemas usados por MCP e drivers OpenAI-compatible vêm de `runtime/drivers/tool_schemas.py`. A arquitetura de aprovação, escopos temporários, contexto confiável e locks de concorrência está detalhada em [Aprovação e segurança de ferramentas](aprovacao.md).

## MCP

O pacote `runtime/mcp` contém:

- `server.py`: servidor JSON-RPC/MCP e proxy stdio-socket;
- `http_server.py`: transporte HTTP/SSE/Streamable;
- `session.py`: runtime MCP embutido da sessão;
- `__main__.py`: entrypoint `python -m quimera.runtime.mcp`.

## Persistência

`Workspace` separa dados persistentes por hash do cwd. `SessionStorage` gerencia histórico/snapshots. `TaskRepository` encapsula SQLite e eventos de domínio. `ConfigManager` trata preferências globais; `RuntimeSecrets` separa segredos privados do runtime (`secrets.env`, com fallback para o `.env` legado) do ambiente operacional do projeto (`<workspace>/.quimera/.env`). O hash do workspace continua restrito ao armazenamento interno e não é usado para configuração humana do projeto. A fronteira de execução é aplicada pelo consumidor: cada caller monta o ambiente tratado antes de usar `process_factory`, que permanece um adapter neutro sobre `subprocess`. Para processos de agentes, `bwrap` mascara apenas arquivos persistentes sensíveis (`.env`, `secrets.env`, conexões, OAuth e configs MCP), preservando o acesso ao histórico/memória do storage.

## UI

Há um único contrato de renderer, `RendererBase` (`ui/base.py`), e dois renderers que o implementam:

- `TextualRenderer` (`ui/textual/renderer.py`): a TUI. O `cli` sempre o injeta quando stdin é um terminal; ele converte cada chamada do contrato em `TextualUiEvent` e a `TextualUiBridge` entrega os eventos ao app Textual, que é dono do terminal.
- `PlainTextRenderer` (`ui/plain.py`): texto puro em stdout, uma linha por mensagem, sem cores nem transitórios. É o renderer de stdin em pipe (`echo "..." | quimera`), do `--interactive-test` e o padrão de `QuimeraApp` quando ninguém passa `renderer_override` (embedding, testes).

Temas vivem em `themes.py`; a TUI os aplica, o renderer de texto só guarda o nome para `/theme` persistir. Com `--debug`, `ui/audit.py` grava eventos estruturados em `data/logs/render/render-<sessão>.jsonl`: a TUI registra `tui_*` e os serviços de exibição registram os seus; o renderer de texto grava um `print` por linha escrita. Não há stream ANSI capturado: nenhum dos dois escreve o terminal por um console interceptável. Handlers de stderr são prompt-aware para evitar quebrar input interativo quando logs assíncronos chegam.

### Usuário usando MCP

A interface do usuário usa `AgentGateway` / `AgentClient` para comunicar com o driver OpenAI-compatible subjacente, através de MCP para agentes externos ou streams nativos para execução local. O renderizador aplica themas que podem ser alterados via `UIThemes` ou variáveis de ambiente.
