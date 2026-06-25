# Instalação e execução

## Requisitos

- Python **3.10 ou superior**.
- `pip` e ambiente virtual recomendado.
- CLIs dos agentes que você pretende usar, por exemplo `claude`, `codex`, `gemini` ou `opencode`.
- Para drivers OpenAI-compatible, variáveis de API configuradas conforme o provedor.

Dependências Python base:

- `rich>=14.0` para renderização terminal;
- `prompt-toolkit>=3.0` para input interativo com histórico e autocomplete;
- `openai>=1.0` para drivers OpenAI-compatible. Se qualquer dependência base estiver ausente (`rich`, `prompt-toolkit` ou `openai`), a CLI encerra antes de iniciar o app por instalação incompleta.

Dependências opcionais:

- `mkdocs>=1.6` no extra `docs` para esta documentação.

## Instalar em modo editável

```bash
git clone git@github.com:alexsilva/quimera.git
cd quimera
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Com suporte à documentação local:

```bash
pip install -e ".[docs]"
```

## Executar

Após instalar, use o entrypoint:

```bash
quimera
```

Também é possível chamar a CLI diretamente em desenvolvimento:

```bash
python -c 'from quimera.cli import main; main()'
```

## Primeiro uso recomendado

1. Liste conexões persistidas:

   ```bash
   quimera --list-connections
   ```

2. Configure um agente dinâmico ou sobrescreva um profile existente:

   ```bash
   quimera --connect meu-agente --driver openai --model gpt-4o --base-url https://api.openai.com/v1 --api-key-env OPENAI_API_KEY
   ```

3. Inicie a sessão escolhendo agentes:

   ```bash
   quimera --agents claude codex gemini --threads 2 --visibility summary
   ```

4. No chat, use `/help`, `/agents`, `/prompt codex` e `/task <descrição>` para explorar.

## Executar com MCP HTTP

Por padrão, o Quimera inicia MCP por socket Unix temporário. Para expor Streamable HTTP local:

```bash
quimera --mcp-http --mcp-host 127.0.0.1 --mcp-port 9090
```

O endpoint principal é `/mcp`. O servidor também expõe `/health`.

## Desativar MCP

```bash
quimera --no-mcp
```

Use apenas quando quiser uma sessão sem ferramentas do runtime expostas aos agentes.
