# Solução de problemas

## O agente selecionado não roda

Verifique se a CLI está instalada e no `PATH`:

```bash
which claude
which codex
which gemini
which opencode
```

Também confira conexões persistidas:

```bash
quimera --list-connections
```

Se um override estiver errado, remova com `/disconnect <agente>` ou edite a conexão com `--connect`.

## MCP não aparece no agente

1. Confirme que você não iniciou com `--no-mcp`.
2. Rode com `--debug` para ver logs do servidor MCP.
3. Para HTTP, confira `/health` no host/porta configurados.
4. Para socket Unix, verifique se o profile selecionado sabe injetar MCP.
5. No HTTP, confirme que o cliente concluiu o fluxo OAuth e recebeu um access token. Não há token estático alternativo para o transporte HTTP.

## MCP client externo não conectou no boot

As conexões `--mcp-client` fecham o handshake em background: a interface sobe
mesmo que um servidor esteja lento, exigindo autorização OAuth ou fora do ar.

1. Olhe o bloco de status no início do chat: cada conexão aparece como
   `aguardando`, `conectando…`, `conectado · N tool(s)` ou `falha — <erro>`.
   Não há linha de resumo: cada servidor mostra o próprio estado.
2. Para `remote:`, uma autorização pendente aparece como
   `conectando… · autorização pendente no navegador`, seguida de uma linha
   com a URL completa (clicável na TUI). Veja "Autorização OAuth pendente"
   abaixo; a conexão completa sozinha depois da confirmação.
3. Falhas mostram no bloco um resumo de uma linha (a exceção mais a linha
   mais informativa do stderr do servidor). O comando completo e o stderr
   inteiro ficam apenas no log do app, cujo caminho aparece no próprio bloco
   quando há falha (`/tmp/quimera/<hash>/data/logs/app-<sessão>.log`). Corrija
   a causa (rede, token, comando) e use o MCP Hub (`F9` → aba Servidores →
   Reconectar) sem reiniciar.
4. As tools de uma conexão só entram no `tools/list` dos agentes depois que ela
   conecta; agentes que já listaram as tools antes disso precisam listar de novo.
5. O catálogo de cada conexão é lido uma única vez, no handshake. Se o servidor
   passar a expor tools novas, use Reconectar no MCP Hub. Em compensação,
   iniciar uma execução nunca espera por um servidor externo: o fork do
   runtime registra as tools a partir do catálogo já conhecido, em qualquer
   perfil de agente (CLI, API ou cloud).

## Tool de MCP externo não responde

Uma chamada `tools/call` a um servidor externo que emudece termina sozinha em
até 5 minutos, com erro de timeout devolvido ao agente. Cancelar o prompt
interrompe a espera na hora, em qualquer perfil, e encerrar o Quimera mata os
servidores externos (ex.: `mcp-remote`) antes de sair, então uma conexão presa
não segura mais o `/exit`. Se o servidor costuma demorar mais que isso por
desenho, abra uma issue: o prazo é fixo em `MCP_CLIENT_TOOL_CALL_TIMEOUT_SECONDS`.

### Autorização OAuth pendente

O `mcp-remote` roda dentro do sandbox do workspace e não consegue abrir o
navegador; quem abre é o Quimera, uma vez por URL, assim que ela chega. Se
nada abrir (sem `DISPLAY`/`WAYLAND_DISPLAY`, sessão SSH, ou
`QUIMERA_OPEN_BROWSER=0`), use uma destas saídas:

- Clique na URL do bloco de status (ou Ctrl+clique, nos terminais com
  hyperlinks): o Quimera abre o navegador ou, se não conseguir, copia o link
  para a área de transferência via OSC 52.
- No MCP Hub (`F9` → aba Servidores), selecione a conexão: a linha
  `Autorizar no navegador` / `Copiar link` aparece enquanto a autorização
  está pendente, e o rodapé mostra a URL completa.

Reconectar pelo hub durante uma autorização pendente abandona o handshake
preso (o processo antigo é encerrado) e começa outro, com uma URL nova; o
hub continua utilizável e pode ser fechado enquanto o servidor espera.
`Desconectar` cancela o handshake sem tentar de novo.

### `EROFS: read-only file system` com o sandbox ativo

Com o sandbox do workspace ligado, o `$HOME` do servidor stdio é somente
leitura. O `mcp-remote` precisa gravar tokens OAuth em `MCP_REMOTE_CONFIG_DIR`
(padrão `~/.mcp-auth`); o Quimera libera esse diretório automaticamente para
conexões `remote:` e para comandos `stdio:` que invocam `mcp-remote`. Se um
outro servidor stdio falhar com `EROFS`, o resumo no bloco termina em
`escrita bloqueada pelo sandbox do workspace`: rode-o fora do sandbox
(`/sandbox off`) ou faça-o gravar dentro do workspace ou de `/tmp`.

## Tool de shell foi bloqueada

O runtime aplica allowlist e denylist. Comandos perigosos ou fora da política são recusados. Prefira comandos pequenos e objetivos (`python`, `pytest`, `git`, `sed`, `find`, `head`, `tail`) e evite operações destrutivas.

## Mutação pendente de aprovação

Use:

```text
/approve
```

para liberar apenas a próxima mutação, ou:

```text
/approve-all
```

para liberar todas as próximas mutações da sessão. Revise o risco antes de usar autoaprovação.

## Contexto errado ou antigo

- Use `/context` para ver o contexto atual.
- Use `/context-edit` para corrigir.
- Use `/context-branch <nome>` para separar iniciativas.
- Use `/reset state` para limpar estado operacional; `/reset history` para limpar o histórico; `/reset all` para ambos.

## Tasks presas

1. Liste tasks via agente/tool ou inspecione `tasks.db`.
2. Confira agentes ativos com `/agents`.
3. Use `/reload` se adicionou/removou conexão.
4. Verifique se há outro agente elegível para failover/review.

## UI ou prompt desorganizado

Rode com `--debug`: o audit de render fica em `data/logs/render/render-<sessão>.jsonl` (eventos `tui_resize`, `tui_clear_chat`, `tui_scroll_clamped`, `tui_screenshot` na TUI; `print` por linha em modo pipe) e os agentes ganham a tool `tui_screenshot`, que devolve a tela atual em texto com a geometria do feed. Use a captura antes de teorizar sobre layout: é o que o humano vê.

## Saída de ferramentas sem o estilo esperado

Na TUI o estilo vem dos renderables Textual (`ui/textual/renderables.py`) e do tema ativo (`/theme`). Em modo pipe não há estilo: o `PlainTextRenderer` escreve texto puro, sem cores nem `dim`, de propósito, para `tee`, logs e testes lerem a saída na ordem. Se um atributo some só na TUI, compare o renderable com a captura de `tui_screenshot` do mesmo turno.
