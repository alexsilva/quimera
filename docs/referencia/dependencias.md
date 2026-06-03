# Dependências

## Python

O projeto requer Python `>=3.10`.

## Dependências base

| Pacote | Motivo |
|---|---|
| `rich>=14.0` | Renderização terminal, painéis, markdown e estilos. |
| `prompt-toolkit>=3.0` | Input interativo, histórico e autocomplete quando disponível. |

## Extras do projeto

| Extra | Pacotes | Uso |
|---|---|---|
| `api` | `openai>=1.0` | Drivers OpenAI-compatible remotos. |
| `ollama` | `openai>=1.0` | Ollama local via API compatível com OpenAI. |
| `docs` | `mkdocs>=1.6` | Build e servidor local desta documentação. |

Instalação completa para desenvolvimento de docs e APIs:

```bash
pip install -e ".[api,ollama,docs]"
```

## Dependências externas não Python

| Ferramenta | Quando precisa |
|---|---|
| `claude` CLI | Para usar plugin Claude. |
| `codex` CLI | Para usar plugin Codex. |
| `gemini` CLI | Para usar plugin Gemini. |
| `opencode` CLI | Para usar plugin OpenCode. |
| Ollama | Para usar `ollama-granite4` local. |
| `bubblewrap` (`bwrap`) | Para fluxos que usam sandbox por bubblewrap. |
| `git`, `python`, `pytest`, `sed`, `find` etc. | Usados por agentes via ferramentas de shell permitidas. |

## Observações

- Nem todo agente precisa estar instalado para iniciar o Quimera, mas a execução falhará se você selecionar um plugin cujo comando não existe.
- Para APIs, configure a variável indicada em `--api-key-env` antes de iniciar a sessão.
- `requirements.txt` contém as dependências de runtime simples; prefira `pip install -e .` para respeitar `pyproject.toml`.
