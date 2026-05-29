"""Entidade para configuração interativa de conexões de agentes."""
import json
import shlex

from .plugins.base import CliConnection, OpenAIConnection


class ConnectionConfigurator:
    """Coleta configuração de conexão interativamente.

    Parâmetros injetados permitem reusar a mesma lógica no CLI (setup inicial)
    e no /connect em runtime, com diferentes canais de I/O e tratamento de erro.

    Args:
        prompt_text: callable(label, default=None) -> str
        prompt_bool: callable(label, default=False) -> bool
        warn: callable(message) — reporta avisos/erros ao usuário
        get_plugin: callable(name) -> plugin | None — para resolução de plugin base (opcional)
    """

    def __init__(self, prompt_text, prompt_bool, warn, get_plugin=None):
        self._prompt_text = prompt_text
        self._prompt_bool = prompt_bool
        self._warn = warn
        self._get_plugin = get_plugin

    def configure(self, plugin, driver_hint: str | None = None):
        """Configura conexão interativamente. Retorna Connection.

        Raises:
            ValueError: se o usuário cancelar ou fornecer dados inválidos.
        """
        current = plugin.effective_connection()
        current_driver = "cli" if isinstance(current, CliConnection) else "openai"
        driver = (driver_hint or self._prompt_text("Driver", current_driver)).strip().lower()
        while driver not in {"cli", "openai"}:
            self._warn("Driver inválido. Use 'cli' ou 'openai'.")
            driver = self._prompt_text("Driver", current_driver).strip().lower()

        if driver == "cli":
            return self._configure_cli(plugin, current)
        return self._configure_openai(plugin, current)

    def configure_with_base(self, plugin):
        """Pergunta por plugin base antes de configurar a conexão.

        Retorna (connection, base_plugin_name | None).

        Raises:
            ValueError: se plugin base não encontrado, modelo vazio, ou cancelamento.
        """
        if self._get_plugin is not None:
            base_name = self._prompt_text("Plugin base (enter para ignorar)", "").strip().lower()
            if base_name:
                base_plugin = self._get_plugin(base_name)
                if base_plugin is None:
                    raise ValueError(f"Plugin base '{base_name}' não encontrado.")
                model_id = self._prompt_text("Modelo", "").strip()
                if not model_id:
                    raise ValueError("Configuração cancelada: modelo vazio.")
                return base_plugin.configure_with_model(model_id), base_plugin.name

        return self.configure(plugin), None

    def _configure_cli(self, plugin, current):
        cli_defaults = current if isinstance(current, CliConnection) else CliConnection(cmd=list(plugin.cmd))

        # Prompt output_format showing current value as default
        output_fmt_default = cli_defaults.output_format or ""
        output_fmt = self._prompt_text(
            "Formato de saída (enter para manter o atual)",
            output_fmt_default,
        ).strip()
        if not output_fmt:
            output_fmt = cli_defaults.output_format  # keep previous (may be None)
        elif output_fmt.lower() in ("nenhum", "none", ""):
            output_fmt = None

        # Prompt cmd showing current value as default
        cmd_default = shlex.join(cli_defaults.cmd) if cli_defaults.cmd else ""
        cmd_text = self._prompt_text("Comando", cmd_default)
        if not cmd_text:
            if cli_defaults.cmd:
                return cli_defaults
            raise ValueError("Configuração cancelada: comando CLI vazio.")

        new_conn = CliConnection(
            cmd=shlex.split(cmd_text),
            prompt_as_arg=self._prompt_bool("Enviar prompt como argumento", cli_defaults.prompt_as_arg),
            output_format=output_fmt,
            env=cli_defaults.env,
            cwd=cli_defaults.cwd,
        )
        # Preserve existing connection if unchanged to avoid unnecessary reloads
        if isinstance(current, CliConnection) and new_conn == current:
            return current
        return new_conn

    def _configure_openai(self, plugin, current):
        # Build defaults based on current connection or plugin defaults
        if isinstance(current, OpenAIConnection):
            api_defaults = current
        else:
            raw_provider = plugin.driver if plugin.driver != "cli" else "openai_compat"
            provider_normalized = "openai_compat" if raw_provider == "openai" else raw_provider
            api_defaults = OpenAIConnection(
                model=plugin.model or "gpt-4o",
                base_url=plugin.base_url or "https://api.openai.com/v1",
                api_key_env=plugin.api_key_env or "OPENAI_API_KEY",
                provider=provider_normalized,
                supports_native_tools=plugin.supports_tools,
                extra_body=getattr(plugin, "extra_body", None),
            )

        # --- Provider ---
        provider_default = api_defaults.provider
        provider = self._prompt_text("Provider", provider_default).strip().lower()
        if not provider:
            provider = provider_default

        # --- Model ---
        model = self._prompt_text("Modelo", api_defaults.model) or api_defaults.model

        # --- Base URL ---
        base_url = self._prompt_text("Base URL", api_defaults.base_url) or api_defaults.base_url

        # --- API Key env var ---
        api_key_env = self._prompt_text("Variável da API key", api_defaults.api_key_env) or api_defaults.api_key_env

        # --- extra_body (JSON) ---
        current_extra_str = ""
        if api_defaults.extra_body:
            current_extra_str = json.dumps(api_defaults.extra_body, ensure_ascii=False)
        extra_body_raw = self._prompt_text(
            "extra_body (JSON, enter para manter o atual)",
            current_extra_str,
        ).strip()
        extra_body = None
        if extra_body_raw:
            # If user typed exactly the current value, keep the original object
            if extra_body_raw == current_extra_str and api_defaults.extra_body is not None:
                extra_body = api_defaults.extra_body
            else:
                try:
                    extra_body = json.loads(extra_body_raw)
                    if extra_body == {}:
                        extra_body = None
                except json.JSONDecodeError as exc:
                    self._warn(f"JSON inválido: {exc}. Mantendo valor atual.")
                    extra_body = api_defaults.extra_body
        else:
            extra_body = api_defaults.extra_body

        # --- supports_native_tools ---
        supports_tools = self._prompt_bool("Suporte a ferramentas nativas", api_defaults.supports_native_tools)

        # --- max_connections ---
        try:
            mc_raw = self._prompt_text("Máximo de conexões concorrentes", str(api_defaults.max_connections)).strip()
            max_connections = int(mc_raw) if mc_raw else api_defaults.max_connections
        except (ValueError, TypeError):
            max_connections = api_defaults.max_connections

        new_conn = OpenAIConnection(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            provider=provider,
            supports_native_tools=supports_tools,
            extra_body=extra_body,
            max_connections=max_connections,
        )
        # Preserve existing connection if unchanged to avoid unnecessary reloads
        if isinstance(current, OpenAIConnection) and new_conn == current:
            return current
        return new_conn