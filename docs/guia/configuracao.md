# Configuração e operação

## Arquivos globais

O Quimera procura um diretório gravável entre locais candidatos do usuário e grava configurações globais nele.

| Arquivo | Conteúdo |
|---|---|
| `config.json` | Nome do usuário, janela de histórico, tema, densidade, idle timeout, tempo máximo do agente, visibilidade, threads, seleção de agentes e roteamento congelado/orquestrador. |
| `connections.json` | Overrides e agentes dinâmicos criados por `--connect`. |
| `mcp-connections.json` | Catálogo global de servidores MCP externos; cada workspace escolhe quais ativar. |
| `mcp-server.json` | Padrões do servidor MCP HTTP (host, porta e perfil de tools). |
| `state/mcp-server.json` | Estado OAuth global do servidor MCP HTTP: cadastros de clients e tokens. |
| `secrets.env` | Segredos globais do runtime, como chaves de providers/modelos. Não são propagados para agentes ou shell. |
| `.env` | Fonte global legada de segredos, ainda lida pelo runtime para compatibilidade. |

Variáveis operacionais específicas de um projeto ficam em
`<workspace>/.quimera/.env`. Esse arquivo é deliberadamente disponibilizado
aos agentes e ferramentas daquele workspace. Por exemplo, um projeto pode
declarar `AWS_PROFILE` ou credenciais de um ambiente de testes sem torná-las
globais para todos os workspaces.

O armazenamento interno em `~/.local/share/quimera/workspaces/<hash>/` guarda
histórico, memória, logs e estado do projeto. Na raiz desse diretório ficam
também os overlays MCP e o `config.json` do workspace; nesse caso, o hash do
caminho seleciona deliberadamente a configuração daquele projeto.

As conexões a servidores MCP externos ficam em dois `mcp-connections.json`: o
catálogo global, no diretório base, e o do workspace, com as conexões do
catálogo ativadas ali (`mcp_active`) e as conexões locais. A configuração do
servidor MCP HTTP segue o mesmo par em `mcp-server.json`, separada do estado
OAuth em `state/mcp-server.json`. Escopos, precedência e o uso pelo MCP Hub
estão em [MCP e ferramentas](mcp-e-ferramentas.md#arquivos-persistentes).
Servidores e variáveis de ambiente são persistidos juntos: no MCP Hub, o editor
de servidor abre com as variáveis em vigor da conexão e grava exatamente o que
estiver no campo Ambiente (vazio remove as variáveis). Atualizações de
configuração e conexões usam substituição atômica e locks entre processos;
arquivos `.lock` adjacentes coordenam essas gravações e podem permanecer no
diretório.

Se um JSON estiver inválido, ilegível ou não contiver um objeto, a leitura usa
defaults e registra um aviso. Novas alterações recusam sobrescrever o arquivo:
corrija seu conteúdo antes de salvar novamente. Uma falha de gravação preserva
o arquivo anterior.

## Configurações persistíveis pela CLI

```bash
quimera --set-theme panel
quimera --set-history-window 20
```

Temas disponíveis na CLI incluem `panel`, `chat`, `rule`, `minimal`, `card` e `line`.

## Variáveis úteis

| Variável | Uso |
|---|---|
| `OPENAI_API_KEY` | Chave padrão para conexões OpenAI-compatible; normalmente em `secrets.env`. |
| `QUIMERA_MCP_TOKEN` | Token opcional do entrypoint MCP socket standalone; não autentica o MCP HTTP. |
| `QUIMERA_MAX_STDERR_LINES` | Ajusta limite de linhas de stderr exibidas em resumo. |
| `SHELL` | Shell usado por ferramentas de comando quando não especificado. |

## Visibilidade de execução

`--visibility` controla como stdout/stderr de agentes aparecem:

- `quiet`: em todos os drivers e perfis, exibe chamadas/conclusões de ferramentas
  e erros, ocultando raciocínio, mensagens intermediárias e previews extensos;
- `summary`: início/fim e resumo operacional;
- `full`: stdout e stderr completos.

Sem a flag, vale o valor salvo em `config.json` (ajustável pela janela
`/config`); a flag é um override apenas da sessão.

## Sandbox do workspace

O sandbox de workspace pode ser controlado pelo seletor **Sandbox do
Workspace** ou por `/sandbox on|off`. A opção é persistida separadamente para
cada workspace. Quando ativa, execuções de agentes, shell, Git e clientes MCP
stdio ficam confinadas ao workspace, `/tmp` e aos diretórios de runtime já
declarados pelos profiles. Clientes MCP baseados em `mcp-remote` também
recebem o diretório de credenciais OAuth (`MCP_REMOTE_CONFIG_DIR`, padrão
`~/.mcp-auth`) como exceção de escrita, para que o login não falhe com
`EROFS`. Como o `mcp-remote` confinado não alcança o navegador, o próprio
Quimera abre a URL de autorização OAuth (uma vez por URL); defina
`QUIMERA_OPEN_BROWSER=0` para desligar e usar o link do bloco de status ou o
botão `Autorizar no navegador` do MCP Hub. A ativação falha de forma segura
quando o `bubblewrap` não está instalado ou não funciona no sistema.

## Paralelismo e timeouts

- `--threads N` limita quantos agentes rodam em paralelo por rodada; sem a
  flag, vale o valor salvo em `config.json`.
- `--idle-timeout N` define o limite de silêncio do agente, sem stdout.
- Na janela `/config`, **Tempo Máximo do Agente** limita a duração total de
  cada execução, incluindo o tempo gasto em tools. O padrão é 3.600 segundos.

## Diretório de trabalho

A sessão usa o diretório atual como workspace. Para REPL de driver, `--working-dir DIR` muda o cwd usado no teste do driver.

O ambiente de subprocessos é composto pelo ambiente normal do processo mais
`<workspace>/.quimera/.env`, removendo antes as chaves provenientes dos
arquivos globais de segredos e os nomes de credencial declarados pelos
providers configurados. Assim uma API key exportada no shell também não é
entregue ao agente por acidente. Não há allowlist fixa por nome: a visibilidade
decorre da origem da configuração e do contrato do provider. Uma chave só volta
a ficar visível quando é declarada explicitamente no `.quimera/.env` do projeto.

Quem inicia um subprocesso prepara o `env` antes de chamar `process_factory`.
A camada de processo é deliberadamente neutra: não filtra, adiciona nem remove
variáveis de ambiente. Git, curl, MCP stdio, editor/clipboard e o worker do
navegador recebem um ambiente já tratado pela camada que conhece o contexto e
a política de visibilidade daquele processo.

Para agentes com acesso ao filesystem, o `bwrap` aplica uma segunda barreira:
arquivos privados do runtime (`.env`, `secrets.env`, `connections.json*`, o
estado OAuth, o `config.json` do workspace e os `mcp-connections.json` global e
do workspace) são sobrepostos por
`/dev/null`. O restante de `~/.local/share/quimera`, incluindo histórico e
memória, continua acessível conforme a policy do agente. A máscara não cria
arquivo temporário nem estado global. Sem `bwrap`, permanece apenas o isolamento
do ambiente; a máscara de filesystem não pode ser aplicada.

## Operação recomendada

Para trabalho diário:

```bash
quimera --agents claude codex --threads 2 --visibility summary
```

Para investigação detalhada:

```bash
quimera --agents claude codex gemini --visibility full --debug
```

Para ambiente com cliente MCP HTTP externo:

```bash
quimera --mcp-http
```

As flags MCP HTTP explícitas valem só para essa sessão e não alteram o
`mcp-server.json`. Para o servidor subir sempre com o workspace, ative-o no MCP
Hub (`F9`), que grava a escolha, o host, a porta e o perfil por escopo, e
libera por workspace os clients OAuth que podem usá-lo.
