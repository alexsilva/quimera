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

Rode com `--debug` e colete logs em `data/logs/render/`. Testes de baseline visual existem em `tests/fixtures/ui_baseline/` e ajudam a comparar regressões.

## Estilo dim/muted ausente do output de ferramentas

**Sintomas:** Mensagens do assistente e output de ferramentas não aparecem com opacidade reduzida (`dim`) como esperado.

**Causa provável:** Mudanças recentes em `renderer.py` (401, 1611-1613) que quebraram o pipeline de estilo.

**Passos de diagnóstico:**

1. Execute o app no modo de teste:
   ```bash
   python quimera.py --test --agents fake-openai --visibility full 2>&1 | rg -i "TOOLS:"
   ```

2. Procure por estilo `dim` ANSI (`\033[2m`) no output. Se estiver ausente:
   - **Problema 1:** Terminal pode não suportar SGR 2
   - **Problema 2:** Estilo pode estar perdido entre `show_plain` e o writer
   - **Problema 3:** `Live`/`TransientOverlay` pode estar sobrepondo com seu próprio estilo

3. Para inspecionar o pipeline de estilo, adicione logging debug no `TerminalRenderer`:

   ```python
   # Em quimera/ui/renderer.py:
   def debug_show_plain(self, message, style=None, **kwargs):
       logger.debug(f"show_plain: message={repr(message[:50])}..., style={style}")
       super().show_plain(message, style, **kwargs)
   ```

4. Arquivos relevantes:
   - `quimera/ui/renderer.py:401` - `build_replace` aplica `\033[2m...\033[0m`
   - `quimera/ui/renderer.py:1611-1613` - `show_turn_summary` passa `muted=True`

**Solução conhecida:** Se o terminal suportar SGR 2, o problema está no pipeline de estilo entre `show_plain` e o writer. Fornecer evidência concreta para localização do bug.
