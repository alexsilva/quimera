# Quimera

Quimera é um orquestrador multiagente para engenharia de software no terminal. Ele permite conversar com agentes especializados, delegar tarefas para execução em background, expor ferramentas por MCP, manter estado compartilhado por workspace e registrar evidências operacionais para auditoria.

## Para quem esta documentação é útil

Use este guia se você precisa:

- instalar e iniciar o Quimera;
- entender quais agentes existem e como conectá-los;
- usar comandos do chat, modos de execução, tasks e review cruzado;
- compreender o runtime de ferramentas, MCP, políticas de aprovação e persistência;
- contribuir no código com segurança e saber onde cada responsabilidade vive.

## Mapa rápido

| Objetivo | Página |
|---|---|
| Entender o produto e seus módulos | [Visão geral](guia/visao-geral.md) |
| Instalar, configurar e rodar | [Instalação e execução](guia/instalacao.md) |
| Operar o chat interativo | [Uso no chat](guia/uso-no-chat.md) |
| Configurar Claude, Codex, Gemini, OpenCode, Ollama ou agentes dinâmicos | [Agentes e conexões](guia/agentes.md) |
| Criar `/task`, acompanhar roteamento, failover e review | [Tasks, roteamento e review](guia/tasks.md) |
| Entender MCP, ferramentas e aprovações | [MCP e ferramentas](guia/mcp-e-ferramentas.md) |
| Localizar arquivos persistidos e contexto | [Estado, memória e evidências](guia/estado-e-memoria.md) |
| Consultar flags e comandos | [Referência de CLI](referencia/cli.md) e [Comandos slash](referencia/comandos.md) |
| Trabalhar no código | [Arquitetura interna](desenvolvimento/arquitetura.md) e [Testes](desenvolvimento/testes.md) |

## Como publicar ou validar a documentação

```bash
pip install -e ".[docs]"
mkdocs serve
```

Para validação não interativa:

```bash
mkdocs build --strict
```
