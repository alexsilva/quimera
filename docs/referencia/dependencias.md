# Dependências

## Python

O projeto requer Python `>=3.10`.

## Dependências base

| Pacote | Motivo |
|---|---|
| `textual>=0.86` | Interface (TUI), incluindo o input. |
| `rich>=14.0` | Renderização de texto, painéis, markdown e estilos. |
| `openai>=1.0` | Drivers OpenAI-compatible remotos e locais. |
| `httpx>=0.27` | Cliente HTTP dos fluxos OAuth (Codex e Claude). |

A CLI valida `rich`, `textual` e `openai` na inicialização e encerra antes de subir o app quando alguma delas está ausente.

## Extras do projeto

| Extra | Pacotes | Uso |
|---|---|---|
| `docs` | `mkdocs>=1.6` | Build e servidor local desta documentação. |
| `browser` | `playwright>=1.50` | Ferramentas `browser_*` (Chrome/Chromium). |
| `oauth-store` | `cryptography>=42` | Criptografia Fernet do store OAuth (`QUIMERA_MCP_OAUTH_STORE_KEY`). |

Instalação para desenvolvimento da documentação:

```bash
pip install -e ".[docs]"
```

Para criptografar o store OAuth em disco:

```bash
pip install -e ".[oauth-store]"
# ou apenas: pip install cryptography
export QUIMERA_MCP_OAUTH_STORE_KEY='passphrase-longa-e-secreta'
```

## Dependências externas não Python

| Ferramenta | Quando precisa |
|---|---|
| `claude` CLI | Para usar profile Claude. |
| `codex` CLI | Para usar profile Codex. |
| `gemini` CLI | Para usar profile Gemini. |
| `opencode` CLI | Para usar profile OpenCode. |
| Ollama | Para usar `ollama-granite4` local. |
| `bubblewrap` (`bwrap`) | Para fluxos que usam sandbox por bubblewrap. |
| `git`, `python`, `pytest`, `sed`, `find` etc. | Usados por agentes via ferramentas de shell permitidas. |

## Observações

- Nem todo agente precisa estar instalado para iniciar o Quimera, mas a execução falhará se você selecionar um profile cujo comando não existe.
- Para APIs, configure a variável indicada em `--api-key-env` antes de iniciar a sessão.
- `requirements.txt` contém as dependências de runtime simples; prefira `pip install -e .` para respeitar `pyproject.toml`.
