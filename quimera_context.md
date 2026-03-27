# Contexto persistente do Quimera

Este arquivo deve ser carregado no inicio de toda execucao do `quimera.py`.
Ele existe para preservar decisoes estaveis sem reinjetar historico bruto no prompt.

## Regras operacionais

- Carregar sempre este arquivo como contexto inicial.
- Nao carregar `quimera_conversa_inicial.txt` nem outros logs brutos como prompt recorrente.
- O historico bruto deve ficar apenas para consulta humana e auditoria.
- Este arquivo deve permanecer curto e curado, com alvo de 80 a 120 linhas no maximo.
- Quando uma decisao ficar obsoleta, substituir a anterior em vez de acumular versoes.

## Decisoes atuais

- O contexto recorrente oficial fica em `quimera_context.md`.
- Transcricoes completas ficam separadas do contexto recorrente.
- O objetivo do contexto persistente e evitar reexplicacao, sem aumentar custo, latencia e ruido.
- Claude e Codex podem usar este arquivo como base para continuar decisoes anteriores.

## O que e o Quimera

- Chat multiagente local em terminal.
- Um HUMANO conversa com CLAUDE e CODEX na mesma rodada.
- O segundo agente comenta a resposta do primeiro.
- Objetivo: comparar respostas e colaborar entre agentes no mesmo fluxo.

## Comportamento atual

- Sem prefixo: Claude responde primeiro, depois Codex comenta.
- `/claude <mensagem>`: Claude responde primeiro.
- `/codex <mensagem>`: Codex responde primeiro.
- `/claude` ou `/codex` sem texto: exibe aviso e nao executa a rodada.
- Transcricao textual continua em `logs/sessao-AAAA-MM-DD.txt`.
- Historico estruturado tambem e salvo em `logs/sessao-AAAA-MM-DD-HHMMSS.json`.
- Ao reiniciar, o JSON mais recente e restaurado para `history`.
- No encerramento, um resumo curado da ultima sessao pode substituir a secao dedicada neste arquivo.
- Logs antigos ficam para consulta humana e auditoria, nao como prompt recorrente.

## Pendencias conhecidas

- A restauracao usa o JSON mais recente; ainda nao existe selecao explicita de sessao.
- O prompt enviado aos agentes continua limitado a uma janela recente da conversa, nao ao historico bruto inteiro.
- A sumarizacao depende do comando `claude` estar disponivel no ambiente.

## Arquivos relacionados

- `quimera.py`: script principal do chat multiagente.
- `quimera_conversa_inicial.txt`: historico salvo da conversa inicial, apenas referencia.
- `logs/`: transcricoes de sessoes.

## Resumo da última sessão

_Gerado em 2026-03-27 03:22_

## Resumo da Sessão

### Tópicos Discutidos

1. **Revisão da sessão anterior** — restauração de contexto via `quimera_context.md`, histórico e commits realizados.

2. **Refatoração em classes** — `quimera.py` foi reestruturado com `ContextManager`, `SessionStorage`, `AgentClient`, `PromptBuilder` e `QuimeraApp`. Commits: `0be999d` e `1c71aed`.

3. **Docstrings e correção de acentuação** — adicionadas docstrings mínimas; erros de português corrigidos em todo o arquivo.

4. **Melhorias visuais no terminal** — discussão sobre uso de `rich` para renderizar Markdown, painéis coloridos por agente e separação total entre renderização e persistência. Classe `TerminalRenderer` implementada com fallback para `print` puro.

5. **`requirements.txt`** — criado com `rich` declarado. Versão definida como `>=10.0.0`, mas política de versão não foi totalmente decidida.

6. **Validação do `rich` no ambiente** — ao testar a renderização, constatou-se que `rich` não estava instalado; o fallback estava ativo e o Markdown saía sem formatação.

### Decisões Tomadas

- `rich` é opcional no código, mas declarado em `requirements.txt`
- Persistência (log, JSON, `history`) continua em texto puro; só a exibição usa `rich`
- Versão mínima: `rich>=10.0.0` (provisório)

### Pendências

- `rich` não instalado no ambiente; nenhum agente executou `pip install rich`
- Política de versão do `rich` não finalizada (`>=10.0.0` vs faixa controlada vs pin exato)
- README não documentado com informações sobre a dependência opcional
- Validação visual real do `TerminalRenderer` ainda não feita
- Restauração de sessão ainda usa o JSON mais recente sem seleção explícita
