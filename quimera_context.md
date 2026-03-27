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

_Gerado em 2026-03-27 03:29_

## Resumo da Sessão

### Tópicos Discutidos

- **Melhoria visual do terminal**: discussão sobre como melhorar a leitura das respostas no shell, comparando com o ChatGPT
- **Implementação do `TerminalRenderer`**: nova classe isolando toda a renderização, usando a lib `rich` com fallback para `print` puro
- **Visual definido**: painéis com borda colorida (Claude = azul, Codex = verde), Markdown renderizado, mensagens de sistema em cinza discreto, largura máxima de 96 colunas
- **Dependência `rich`**: decisão de declarar no `requirements.txt` mesmo sendo opcional no código
- **Versão fixada**: `rich==14.3.3` após instalação e confirmação no ambiente
- **Bug de contraste**: nome do agente sumia porque texto e borda tinham a mesma cor; corrigido com badge `white on {style}`

### Decisões Tomadas

- `rich` declarado em `requirements.txt` com versão exata (`rich==14.3.3`)
- Commit `48f0704` consolidou `requirements.txt` e atualização do `quimera_context.md`
- Renderização e persistência permanecem separadas — histórico, logs e JSON continuam em texto puro

### Pendências / Próximos Passos

- Correção do contraste do nome do agente ainda não foi commitada
- `quimera_context.md` pode estar parcialmente desatualizado
- README ainda não documenta a dependência `rich` nem instruções de instalação
