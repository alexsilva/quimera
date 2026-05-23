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
        cmd_default = shlex.join(cli_defaults.cmd) if cli_defaults.cmd else ""
        cmd_text = self._prompt_text("Comando", cmd_default)
        if not cmd_text:
            raise ValueError("Configuração cancelada: comando CLI vazio.")
        return CliConnection(
            cmd=shlex.split(cmd_text),
            prompt_as_arg=self._prompt_bool("Enviar prompt como argumento", cli_defaults.prompt_as_arg),
            output_format=cli_defaults.output_format,
        )

    def _configure_openai(self, plugin, current):
        api_defaults = current if isinstance(current, OpenAIConnection) else OpenAIConnection(
            model=plugin.model or "gpt-4o",
            base_url=plugin.base_url or "https://api.openai.com/v1",
            api_key_env=plugin.api_key_env or "OPENAI_API_KEY",
            provider=plugin.driver if plugin.driver != "cli" else "openai_compat",
            supports_native_tools=plugin.supports_tools,
            extra_body=getattr(current, "extra_body", None),
        )
        provider_default = api_defaults.provider if api_defaults.provider != "openai" else "openai_compat"
        extra_body_raw = self._prompt_text("extra_body (JSON, enter para ignorar)", "").strip()
        extra_body = None
        if extra_body_raw:
            try:
                extra_body = json.loads(extra_body_raw)
                if extra_body == {}:
                    extra_body = None
            except json.JSONDecodeError as exc:
                self._warn(f"JSON inválido: {exc}. extra_body será ignorado.")
                extra_body = api_defaults.extra_body
        else:
            extra_body = api_defaults.extra_body
        return OpenAIConnection(
            model=self._prompt_text("Modelo", api_defaults.model) or api_defaults.model,
            base_url=self._prompt_text("Base URL", api_defaults.base_url) or api_defaults.base_url,
            api_key_env=self._prompt_text("Variável da API key", api_defaults.api_key_env) or api_defaults.api_key_env,
            provider=provider_default,
            supports_native_tools=api_defaults.supports_native_tools,
            extra_body=extra_body,
        )
