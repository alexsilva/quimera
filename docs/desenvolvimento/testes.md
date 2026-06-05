# Testes e qualidade

## Rodar testes

```bash
pytest
```

Para um arquivo específico:

```bash
pytest tests/test_task_router.py
```

Para documentação:

```bash
mkdocs build --strict
```

## Áreas cobertas por testes

A suíte inclui testes para:

- protocolo e servidor MCP;
- ferramentas de runtime: arquivos, shell, patch, tasks e web;
- parser, drivers e integração OpenAI-compatible;
- roteamento, execução, review e failover de tasks;
- prompt, modos, contexto, estado compartilhado e TTL;
- app interativa, handlers, dispatch e turn management;
- UI, temas, wrapping e fixtures de baseline visual;
- configuração, plugins, conexões e workspace;
- evidências, storage, métricas e bugs.

## Fixtures visuais

`tests/fixtures/ui_baseline/` contém snapshots textuais por tema/densidade/largura. Ao alterar renderização, rode testes de UI e revise diffs com cuidado.

## Boas práticas de contribuição

1. Leia `AGENTS.md` antes de editar.
2. Faça mudanças pequenas e localizadas.
3. Prefira serviços existentes a adicionar lógica dentro de `QuimeraApp`.
4. Atualize docs quando alterar comportamento de CLI, comandos, tools, plugins ou persistência.
5. Rode testes relacionados e, quando possível, a suíte completa.
6. Para mudanças perceptíveis na UI, capture evidência visual ou atualize fixtures conscientemente.

## Comandos úteis de inspeção

```bash
rg "CMD_" quimera
rg "class AgentPlugin" quimera/plugins/base.py
rg "def score_plugin_for_task" quimera/runtime/task_planning.py
python - <<'PY'
from quimera.runtime.drivers.tool_schemas import resolve_tool_schemas
print([s['function']['name'] for s in resolve_tool_schemas()])
PY
```

## Testador interativo local com agentes fake

Para validar o fluxo do Quimera sem depender de credenciais ou provedores externos, o projeto inclui mecanismos fake e determinísticos. Esses plugins ficam fora do uso normal: execute o app com `--test` para ativá-los e restringir a rodada aos fake.

### Agente CLI local

Execute um agente local por stdin ou argumento:

```bash
python -m quimera.devtools.fake_agents cli --role tester "valide o roteamento de tarefas"
```

Papéis disponíveis:

- `tester`: emite uma evidência de teste fake.
- `reviewer`: simula uma revisão determinística.
- `architect`: simula uma resposta arquitetural.
- `coder`: responde como executor local simples.

O plugin embutido `fake-cli` usa esse mecanismo e pode participar de uma sessão do app:

```bash
python quimera.py --test --agents fake-cli --visibility full
```

### Backend OpenAI-compatible fake com tool calling

Use o plugin embutido `fake-openai` no REPL do driver. Com `--test`, o app sobe automaticamente o backend OpenAI-compatible fake em uma porta livre e aplica o override somente no processo. Esse caminho usa o driver `openai_compat`, cuja dependência `openai` faz parte da instalação base:

```bash
python quimera.py --test --driver-repl fake-openai --prompt "Leia o README usando ferramentas"
```

O servidor expõe:

- `GET /v1/models` para o probe do driver.
- `POST /v1/chat/completions` em formato OpenAI-compatible.
- Tool calls nativas para `read_file`, `list_files`, `grep_search`, `run_shell` e `write_file` conforme palavras-chave do prompt.

Gatilhos úteis:

| Prompt contém | Tool selecionada |
|---|---|
| `README` | `read_file` |
| `listar`, `arquivos`, `files` ou `ls` | `list_files` |
| `grep`, `buscar`, `procure` ou `search` | `grep_search` |
| `pwd`, `diretório`, `diretorio` ou `shell` | `run_shell` |
| `escreva`, `write` ou `arquivo probe` | `write_file` |

### CLI que delega para um agente OpenAI via MCP

O plugin `fake-cli-handoff` valida o caminho entre dois agentes de execução diferentes: um agente CLI MCP-capaz recebe o socket/token MCP do Quimera por variáveis de ambiente, chama a tool `call_agent` e delega para o agente OpenAI-compatible `fake-openai`. A partir daí, o agente OpenAI usa o driver `openai_compat`, emite tool calls nativas e passa pelo fluxo normal de aprovação/execução de ferramentas do runtime.

O plugin `fake-openai-mcp-cli` continua disponível para validar o caminho direto CLI -> OpenAI-compatible -> MCP, mas ele não exercita delegação entre agentes; para comprovar CLI -> `call_agent` -> OpenAI, prefira `fake-cli-handoff`.

Rode o app com MCP habilitado (padrão) e o agente CLI MCP. Não é necessário iniciar um servidor externo antes: `--test` sobe o backend fake em porta livre e aponta `fake-openai` para ele com override não persistente:

```bash
python quimera.py --test --agents fake-cli-handoff fake-openai --visibility full
```

Prompt de smoke sugerido:

```text
Execute pwd via shell usando o agente OpenAI
```

O output esperado deve conter, nessa ordem, `MCP conectado`, `MCP tool_call: call_agent`, execução do `fake-openai`, aprovação/execução da ferramenta pedida pelo OpenAI e `MCP tool_result: OK`. Esse fluxo comprova que um CLI local consegue delegar para um agente OpenAI por `call_agent`, e que o agente OpenAI executa ferramentas pelo runtime com a política normal de aprovação.

Também é possível executar o app inteiro com o agente OpenAI fake:

```bash
python quimera.py --test --agents fake-openai --visibility full
```

Se `--agents` não for informado, todos os fake agents entram como agentes disponíveis/padrão do modo de teste. Esse modo permite que o operador humano atue como testador interativo: envie comandos no chat, observe previews/aprovações de tools e confira a resposta final com a evidência retornada pelo runtime.

O comando standalone permanece disponível apenas para debug manual e não é pré-requisito do modo `--test`:

```bash
python -m quimera.devtools.fake_agents openai-server
```
