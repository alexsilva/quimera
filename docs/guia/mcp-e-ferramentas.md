# MCP e ferramentas

## Papel do MCP

O Quimera inicia um servidor MCP embutido por sessão para expor ferramentas do runtime aos agentes compatíveis. O servidor usa JSON-RPC 2.0 e negocia versões do protocolo MCP, mantendo compatibilidade com versões anteriores.

Transportes suportados:

- **socket Unix**: padrão, com proxy stdio para CLIs;
- **HTTP Streamable**: ativado por `--mcp-http`, útil para clientes externos locais;
- **stdio standalone**: `python -m quimera.runtime.mcp` para usos isolados.

## Autenticação

Os transportes têm autenticação independente. Clientes do socket Unix enviam uma primeira linha JSON com `quimera_auth_token`; esse token pertence somente ao socket. O transporte HTTP usa exclusivamente OAuth e envia o access token em `Authorization: Bearer <token>`.

## Métodos MCP principais

O servidor implementa, entre outros:

- `initialize` e `notifications/initialized`;
- `tools/list`;
- `tools/call`;
- `ping`;
- recursos, prompts e completion conforme suporte do runtime.

Chamadas de tool podem ser executadas em thread pool, com cancelamento e progresso.

## Ferramentas disponíveis

| Tool | Função |
|---|---|
| `list_files` | Lista arquivos/diretórios dentro do workspace. |
| `read_file` | Lê arquivo com intervalo opcional de linhas. |
| `grep_search` | Busca padrão de texto em arquivos. |
| `inspect_symbols` | Lista classes, funções e métodos de um arquivo Python via AST, sem executar código. |
| `write_file` | Cria ou sobrescreve arquivo; para mudanças parciais, prefira patch. |
| `apply_patch` | Aplica patch textual estruturado no workspace. |
| `replace_text` | Substitui texto literal em um arquivo dentro do workspace. |
| `remove_file` | Remove arquivo com confirmação por `dry_run=False`. |
| `run_shell` | Executa comando shell simples no workspace. |
| `exec_command` | Executa comando com sessão persistente e polling incremental. |
| `write_stdin` | Escreve no stdin de sessão aberta por `exec_command`. |
| `poll_command_session` | Consulta stdout/stderr incremental de sessão aberta, sem escrever no stdin. |
| `close_command_session` | Fecha sessão persistente de comando. |
| `git_status`, `git_diff`, `git_log`, `git_add`, `git_commit`, `git_branch`, `git_checkout`, `git_fetch`, `git_push` | Operações git estruturadas no repositório do workspace. |
| `browser_start`, `browser_navigate`, `browser_click`, `browser_type`, `browser_press`, `browser_mouse`, `browser_wait`, `browser_snapshot`, `browser_screenshot`, `browser_console`, `browser_network`, `browser_evaluate`, `browser_status`, `browser_close` | Automação de navegador (Chrome/Chromium via Playwright, extra `browser`); screenshots são salvos por sessão no diretório de artefatos do workspace. |
| `list_tasks` | Lista tasks com filtros. |
| `list_jobs` | Lista jobs. |
| `get_job` | Obtém detalhes de job. |
| `list_agents` | Lista os agentes ativos na sessão atual. |
| `web_search` | Pesquisa web via DuckDuckGo Lite. |
| `web_fetch` | Busca URL e extrai texto. |
| `todo_write` | Cria/atualiza TODOs da sessão. |
| `todo_list` | Lista TODOs da sessão. |
| `memory_save` | Salva/atualiza entrada estruturada da memória do workspace. |
| `memory_retrieve` | Recupera memória do workspace por namespace, key, prefixo ou tags. |
| `update_shared_state` | Atualiza o shared state da sessão. |
| `ask_user` | Faz uma pergunta ao usuário humano e aguarda resposta. |
| `delegate` | Delega tarefa para outro agente do pool Quimera. |

## Política de segurança

O runtime usa `ToolRuntimeConfig` para definir:

- raiz do workspace e raízes de leitura permitidas;
- timeout de comandos;
- limite de output, leitura de arquivo e resultados de busca;
- exigência de aprovação para mutações;
- allowlist de comandos shell comuns;
- denylist para padrões perigosos como `rm -rf`, `sudo`, `shutdown`, `mkfs`, `dd` e permissões recursivas arriscadas.

## Aprovação

Ferramentas de mutação podem exigir aprovação. Na app interativa, `/approve` libera a próxima mutação e `/approve-all` muda o comportamento para autoaprovação. Em execução não interativa ou MCP standalone, o handler de aprovação pode ser configurado pelo runtime.

## Cross-MCP e `delegate`

A ferramenta `delegate` permite que um agente delegue uma tarefa a outro agente do pool. Ela é útil para dividir trabalho por especialidade: arquitetura para Gemini/Claude, edição para Codex/OpenCode, revisão para agentes fortes em review. O resultado entra no fluxo da sessão e pode ser usado como evidência ou contexto para a resposta final.

Cada delegação executa em um `AgentClient` isolado criado por chamada (dispatch de background), com cancel_event próprio: o agente delegado nunca interfere na execução ativa do agente que delegou, e delegações concorrentes não corrompem estado uma da outra. Cancelar o fluxo principal (ESC/Ctrl+C) também cancela as delegações em andamento. O client isolado herda o comportamento de pausa de idle timeout durante tools longas e o supervisor de processos da sessão, garantindo que subprocessos delegados sejam encerrados no shutdown.

## OAuth 2.1 no MCP HTTP

O Quimera embute um Authorization Server completo no próprio servidor MCP HTTP. Nenhum serviço externo nem banco é necessário. A dependência Python `cryptography` só é exigida se você optar por criptografar o arquivo de persistência do store (ver [Persistência do store](#persistencia-do-store)).

### Ligar em um comando

```bash
quimera --mcp-http
```

Isso é suficiente para um cliente MCP OAuth-aware (Claude, Cursor, ChatGPT tunnel-client) conectar sozinho: ele descobre o servidor por RFC 9728, registra-se dinamicamente por RFC 7591, abre a tela de consentimento no navegador e recebe o token. Não há `client_id` para copiar nem arquivo para editar.

### Endpoints

| Endpoint | RFC | Descrição |
|---|---|---|
| `GET /.well-known/oauth-protected-resource[/mcp]` | RFC 9728 | Declara `/mcp` como recurso protegido e aponta o Authorization Server. |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 | Metadados do AS: endpoints, grants, escopos e PKCE. |
| `GET /oauth/authorize` | RFC 6749 | Tela de consentimento (HTML autocontido). |
| `POST /oauth/authorize` | — | Decisão do usuário (autorizar/negar). |
| `POST /oauth/token` | RFC 6749 | `authorization_code`, `refresh_token`, `client_credentials`. |
| `POST /oauth/register` | RFC 7591 | Registro dinâmico de clients. |
| `POST /oauth/revoke` | RFC 7009 | Revogação de access/refresh token. |
| `POST /oauth/introspect` | RFC 7662 | Introspecção restrita ao client autenticado. |

Os dois endpoints de discovery são **públicos** (não exigem `Authorization`) e não expõem tokens ou segredos. Ao habilitar `--mcp-http`, o Authorization Server OAuth já é iniciado automaticamente.

Requisições não autenticadas em `/mcp` respondem `401` com `WWW-Authenticate` contendo `resource_metadata`, que é o gatilho da auto-descoberta do cliente.

### Garantias do fluxo

- **PKCE `S256` obrigatório** — `plain` só é aceito se `require_pkce=False`.
- **Código de uso único**, com TTL de 5 minutos.
- **Refresh token rotativo**: o anterior é invalidado a cada renovação e não pode ampliar o escopo original.
- **Redirect URI** validado por igualdade exata; portas efêmeras de loopback são aceitas (RFC 8252). `http://` só é permitido em loopback.
- **Audience binding** via parâmetro `resource` (RFC 8707).
- Access tokens são opacos e, enquanto válidos, são persistidos junto com clients dinâmicos e refresh tokens. Reiniciar o servidor não revoga uma autorização ainda válida; tokens expirados ou revogados não são restaurados — ver [Persistência do store](#persistencia-do-store).

### Escopos e perfis de ferramentas

O escopo concedido pode **restringir** o perfil de tools abaixo do configurado em `--mcp-http-allow-tools` (nunca ampliá-lo):

| Escopo | Efeito |
|---|---|
| `mcp` | Herda o perfil configurado no servidor (padrão). |
| `mcp:read-local` | Leitura local, sem acesso à rede. |
| `mcp:read` | Leitura local + `web_search`/`web_fetch`. |
| `mcp:agent` | Leitura, `replace_text`, git e `delegate`. |
| `mcp:all` | Todas as tools publicadas pelo transporte. |

As tools fora do escopo desaparecem de `tools/list` e são recusadas em `tools/call`.

### Opções de configuração

| Flag | Variável de ambiente | Efeito |
|---|---|---|
| `--mcp-oauth-issuer URL` | `QUIMERA_MCP_OAUTH_ISSUER` | URL pública do issuer (necessária atrás de proxy/túnel). |
| `--mcp-oauth-client ID[:SECRET]` | `QUIMERA_MCP_OAUTH_CLIENTS` | Client estático; com `SECRET` habilita `client_credentials`. |
| `--mcp-oauth-redirect-uri URI` | `QUIMERA_MCP_OAUTH_REDIRECT_URIS` | Redirects permitidos aos clients estáticos. |
| `--mcp-oauth-passcode-env VAR` | `QUIMERA_MCP_OAUTH_PASSCODE` | Código exigido na tela de consentimento. |
| `--mcp-oauth-auto-approve` | `QUIMERA_MCP_OAUTH_AUTO_APPROVE=1` | Dispensa o consentimento (só desenvolvimento local). |
| `--mcp-oauth-no-register` | `QUIMERA_MCP_OAUTH_ALLOW_REGISTER=0` | Desliga RFC 7591; exige clients estáticos. |
| `--mcp-oauth-store PATH` | `QUIMERA_MCP_OAUTH_STORE` | Arquivo JSON do estado OAuth persistente. |
| — | `QUIMERA_MCP_OAUTH_STORE_KEY` | Passphrase para criptografar o store em disco (Fernet). |
| — | `QUIMERA_MCP_OAUTH_ACCESS_TTL` | TTL do access token (padrão `3600`). |
| — | `QUIMERA_MCP_OAUTH_REFRESH_TTL` | TTL do refresh token (padrão 30 dias). |

Flags têm precedência sobre o ambiente. `--mcp-oauth-passcode-env` aponta por padrão para `QUIMERA_MCP_OAUTH_PASSCODE`: se a variável estiver definida, o consentimento passa a exigir o código; caso contrário, basta clicar em **Autorizar**. Um passcode configurado tem precedência sobre `--mcp-oauth-auto-approve`.

### Persistência do store

O Authorization Server grava em disco clients registrados dinamicamente, access tokens ainda válidos e refresh tokens. Códigos de autorização e pedidos de consentimento em andamento continuam voláteis.

| Aspecto | Comportamento |
|---|---|
| Caminho padrão | `<base_dir>/state/mcp_oauth.json` (global do app, ex.: `~/.local/share/quimera/state/`; ou `--mcp-oauth-store` / `QUIMERA_MCP_OAUTH_STORE`) |
| Permissões | `0600` após cada gravação atômica |
| Sem `QUIMERA_MCP_OAUTH_STORE_KEY` | JSON em **texto claro**: `client_secret`, access tokens e refresh tokens são legíveis no arquivo |
| Com `QUIMERA_MCP_OAUTH_STORE_KEY` | Payload criptografado com Fernet (prefixo `quimera-oauth-fernet:v1:`); a chave é derivada da passphrase via PBKDF2-SHA256 |
| Dependência | Extra opcional `oauth-store` (`cryptography>=42`). Sem o pacote, a chave é ignorada e o store permanece em claro, com warning no log |
| Store cifrado sem a chave correta | Load vazio (não quebra o servidor; clients/refresh precisam ser recriados) |
| Migração | Arquivo antigo em claro continua legível; na próxima gravação com a chave definida ele é regravado já criptografado |

**Uso recomendado** quando o diretório global do app (`~/.local/share/quimera`) pode ser copiado, sincronizado ou acessado por outros usuários do host:

```bash
pip install 'quimera[oauth-store]'   # ou: pip install cryptography
export QUIMERA_MCP_OAUTH_STORE_KEY='passphrase-longa-e-secreta'
quimera --mcp-http
```

Em uso estritamente local (loopback, disco privado, um único operador), o modo em claro com `0600` costuma ser suficiente. Em qualquer exposição além da máquina local, combine store cifrado com passcode de consentimento e, se possível, `--mcp-oauth-no-register`.

### Exposição pública com túnel

Atrás de proxy, o issuer precisa ser a URL HTTPS externa. Duas formas:

```bash
# 1. Explícita (recomendada)
quimera --mcp-http --mcp-oauth-issuer https://quimera.exemplo.dev

# 2. Automática, se o proxy enviar X-Forwarded-Proto e X-Forwarded-Host
quimera --mcp-http
```

Com exposição pública, defina também `QUIMERA_MCP_OAUTH_PASSCODE` — sem ele, qualquer um que alcance a tela de consentimento pode conceder acesso ao workspace.

### Acesso máquina-a-máquina

Para scripts e CI, use um client confidencial com `client_credentials`:

```bash
quimera --mcp-http --mcp-oauth-client ci-runner:$CI_SECRET

curl -s -X POST http://127.0.0.1:9090/oauth/token \
  -d grant_type=client_credentials \
  -d client_id=ci-runner \
  -d client_secret="$CI_SECRET" \
  -d scope=mcp:read-local
```

O `access_token` retornado vai em `Authorization: Bearer <token>` nas chamadas a `/mcp`.

## ChatGPT Secure MCP Tunnel via HTTP

Clientes OAuth-aware como o `tunnel-client` da OpenAI usam o fluxo OAuth publicado pelo próprio servidor HTTP.

### Configuração do servidor HTTP

Inicie o Quimera com HTTP MCP habilitado e o conjunto de ferramentas `agent`:

```bash
quimera --mcp-http --mcp-port 9095 --mcp-http-allow-tools agent
```

O conjunto `agent` publica apenas:
- `list_files`, `read_file`, `grep_search`, `inspect_symbols`, `list_tasks`, `list_jobs`, `get_job`, `memory_retrieve`, `todo_list` (somente leitura local)
- `git_status`, `git_log`, `git_diff`, `git_branch`, `git_fetch` (git somente leitura)
- `web_search`, `web_fetch` (leitura de rede)
- `delegate`, `list_agents` (delegação para agentes do pool)
- `replace_text`, `memory_save` e git de mutação (`git_add`, `git_commit`, `git_checkout`, `git_push`), sujeitos a aprovação

Ferramentas de escrita ampla e shell (`run_shell`, `write_file`, `apply_patch`, `remove_file`, `exec_command`) **não são expostas** por esse conjunto.

### Validação

```bash
# Checar endpoints de discovery
curl -i http://127.0.0.1:9095/.well-known/oauth-protected-resource/mcp
curl -i http://127.0.0.1:9095/.well-known/oauth-authorization-server

# Verificar saúde do servidor
curl http://127.0.0.1:9095/health
```

### Autenticação

O cliente descobre o Authorization Server, conclui o fluxo OAuth e envia o access token resultante em `Authorization: Bearer <token>`. Tokens estáticos e o header legado `X-Quimera-MCP-Token` não são aceitos pelo transporte HTTP.
