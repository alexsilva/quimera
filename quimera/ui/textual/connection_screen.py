"""Modal Textual para configurar conexões de agentes."""
from __future__ import annotations

import json
import shlex

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Switch

from quimera.profiles.base import CliConnection, OpenAIConnection


class ConnectionScreen(ModalScreen[None]):
    """Edita a conexão de um agente sem usar o input principal do chat."""

    CSS = """
    ConnectionScreen { align: center middle; background: rgba(0, 0, 0, 0.65); }
    #connection_dialog {
        width: 72; height: auto; max-height: 92%;
        background: $surface; border: round $primary; padding: 1 2;
    }
    #connection_title { text-align: center; text-style: bold; color: $accent; margin-bottom: 1; }
    #connection_fields { height: auto; max-height: 62vh; overflow-y: auto; }
    #connection_fields Label { margin-top: 1; color: $text-muted; }
    #connection_buttons { margin-top: 1; height: 1; align-horizontal: right; }
    #connection_buttons Button { height: 1; min-width: 12; border: none; margin-left: 2; }
    #connection_hint { margin-top: 1; width: 100%; text-align: center; color: $text-muted; height: 1; }
    .hidden { display: none; }
    """

    BINDINGS = [("escape", "cancel", "Cancelar"), ("ctrl+s", "save", "Salvar")]
    AUTO_FOCUS = "#conn_driver"

    def __init__(self, quimera_app, parent_app, agent_name: str, *, advanced: bool = False) -> None:
        super().__init__()
        self.quimera_app = quimera_app
        self.parent_app = parent_app
        self.agent_name = agent_name
        self.advanced = advanced
        system_layer = getattr(quimera_app, "system_layer", None)
        profile_resolver = getattr(system_layer, "profile_resolver", None)
        self.profile = profile_resolver.get(agent_name) if profile_resolver is not None else None
        if self.profile is None:
            raise ValueError(f"Agente '{agent_name}' não encontrado para configuração.")
        self.current = self.profile.effective_connection()

    def compose(self) -> ComposeResult:
        is_cli = isinstance(self.current, CliConnection)
        cli = self.current if is_cli else CliConnection(cmd=list(getattr(self.profile, "cmd", []) or []))
        api = self.current if isinstance(self.current, OpenAIConnection) else OpenAIConnection(
            model=getattr(self.profile, "model", None) or "gpt-4o",
            base_url=getattr(self.profile, "base_url", None) or "https://api.openai.com/v1",
            api_key_env=getattr(self.profile, "api_key_env", None) or "OPENAI_API_KEY",
            provider="openai_compat",
            supports_native_tools=bool(getattr(self.profile, "supports_tools", True)),
        )
        with Container(id="connection_dialog"):
            yield Label(f"Conexão · {self.agent_name}", id="connection_title")
            with Vertical(id="connection_fields"):
                yield Label("Driver")
                yield Select([("OpenAI/API", "openai"), ("CLI", "cli")], value="cli" if is_cli else "openai", id="conn_driver")

                with Vertical(id="conn_openai_fields", classes="hidden" if is_cli else ""):
                    yield Label("Provider")
                    yield Input(value=api.provider, id="conn_provider")
                    yield Label("Modelo")
                    yield Input(value=api.model, id="conn_model")
                    yield Label("Base URL")
                    yield Input(value=api.base_url, id="conn_base_url")
                    yield Label("Variável da API key")
                    yield Input(value=api.api_key_env, id="conn_api_key_env")
                    yield Label("Máximo de conexões")
                    yield Input(value=str(api.max_connections), id="conn_max_connections")
                    yield Label("extra_body (JSON; vazio remove)")
                    yield Input(value=json.dumps(api.extra_body, ensure_ascii=False) if api.extra_body else "", id="conn_extra_body")
                    yield Label("Ferramentas nativas")
                    yield Switch(value=api.supports_native_tools, id="conn_native_tools")

                with Vertical(id="conn_cli_fields", classes="" if is_cli else "hidden"):
                    yield Label("Comando")
                    yield Input(value=shlex.join(cli.cmd) if cli.cmd else "", id="conn_command")
                    yield Label("Formato de saída")
                    yield Input(value=cli.output_format or "", id="conn_output_format")
                    yield Label("Enviar prompt como argumento")
                    yield Switch(value=cli.prompt_as_arg, id="conn_prompt_as_arg")

            with Horizontal(id="connection_buttons"):
                yield Button("Cancelar", id="conn_cancel")
                yield Button("Salvar", variant="primary", id="conn_save")
            yield Label("Ctrl+S salva · Esc cancela", id="connection_hint")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "conn_driver":
            return
        is_cli = event.value == "cli"
        self.query_one("#conn_cli_fields").set_class(not is_cli, "hidden")
        self.query_one("#conn_openai_fields").set_class(is_cli, "hidden")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "conn_cancel":
            self.action_cancel()
        elif event.button.id == "conn_save":
            self.action_save()

    def action_cancel(self) -> None:
        self.dismiss()

    def action_save(self) -> None:
        driver = self.query_one("#conn_driver", Select).value
        try:
            if driver == "cli":
                command = self.query_one("#conn_command", Input).value.strip()
                if not command:
                    raise ValueError("O comando CLI não pode ficar vazio.")
                connection = CliConnection(
                    cmd=shlex.split(command),
                    prompt_as_arg=self.query_one("#conn_prompt_as_arg", Switch).value,
                    output_format=self.query_one("#conn_output_format", Input).value.strip() or None,
                )
            else:
                model = self.query_one("#conn_model", Input).value.strip()
                base_url = self.query_one("#conn_base_url", Input).value.strip()
                api_key_env = self.query_one("#conn_api_key_env", Input).value.strip()
                if not model or not base_url:
                    raise ValueError("Modelo e Base URL são obrigatórios.")
                max_connections = int(self.query_one("#conn_max_connections", Input).value.strip())
                if max_connections <= 0:
                    raise ValueError("Máximo de conexões deve ser positivo.")
                extra_raw = self.query_one("#conn_extra_body", Input).value.strip()
                extra_body = json.loads(extra_raw) if extra_raw else None
                if extra_body == {}:
                    extra_body = None
                connection = OpenAIConnection(
                    model=model,
                    base_url=base_url,
                    api_key_env=api_key_env,
                    provider=self.query_one("#conn_provider", Input).value.strip() or "openai_compat",
                    supports_native_tools=self.query_one("#conn_native_tools", Switch).value,
                    max_connections=max_connections,
                    extra_body=extra_body,
                )
        except (ValueError, json.JSONDecodeError) as exc:
            self.parent_app.notify(str(exc), severity="error")
            return

        self.quimera_app.system_layer.apply_connection_configuration(
            self.agent_name,
            connection,
        )
        self.parent_app.notify(f"Conexão de {self.agent_name} salva.", severity="information")
        self.dismiss()
