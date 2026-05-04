"""Componentes de `quimera.cli`."""
import argparse
import json
import locale
import os
import shlex
import sys
from pathlib import Path
from typing import List

from .constants import Visibility
from . import plugins as _plugins
from .plugins.base import (
    CliConnection,
    OpenAIConnection,
    _connection_from_dict,
    connection_to_dict,
    format_connection_label,
    is_valid_agent_name,
    load_connections,
    register_dynamic_plugin,
    save_connections,
    set_connection_override,
)
from . import themes as _themes
from .app import QuimeraApp
from .config import ConfigManager
from .runtime.drivers.repl import DriverRepl
from .workspace import Workspace

try:
    from .ui import TerminalRenderer
    from .agents import AgentClient
except ImportError:
    TerminalRenderer = None
    AgentClient = None


def _expand_patterns(agents: List[str], available: List[str]) -> List[str]:
    """Executa expand patterns."""
    result = []
    seen = set()
    for a in agents:
        a = a.strip().lower()
        if "*" in a:
            prefix = a.replace("*", "")
            matches = [n for n in available if n.startswith(prefix)]
            for n in matches:
                if n not in seen:
                    seen.add(n)
                    result.append(n)
        else:
            if a not in seen:
                seen.add(a)
                result.append(a)
    return result


def _prompt_text(label: str, default: str | None = None) -> str:
    """Lê um valor interativo com default opcional."""
    suffix = f" [{default}]" if default not in {None, ""} else ""
    value = input(f"{label}{suffix}: ").strip()
    if value:
        return value
    return default or ""


def _prompt_bool(label: str, default: bool = False) -> bool:
    """Lê um booleano interativo."""
    default_label = "s" if default else "n"
    while True:
        raw = input(f"{label} [s/n] [{default_label}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"s", "sim", "y", "yes"}:
            return True
        if raw in {"n", "nao", "não", "no"}:
            return False
        print("Valor inválido. Use 's' ou 'n'.")


def _configure_connection_interactively(plugin, driver_hint: str | None = None):
    """Coleta configuração de conexão de forma interativa."""
    current = plugin.effective_connection()
    current_driver = "cli" if isinstance(current, CliConnection) else "openai"
    driver = (driver_hint or _prompt_text("Driver", current_driver)).strip().lower()
    while driver not in {"cli", "openai"}:
        print("Driver inválido. Use 'cli' ou 'openai'.")
        driver = _prompt_text("Driver", current_driver).strip().lower()

    if driver == "cli":
        cli_defaults = current if isinstance(current, CliConnection) else CliConnection(cmd=list(plugin.cmd))
        cmd_default = shlex.join(cli_defaults.cmd) if cli_defaults.cmd else ""
        cmd_text = _prompt_text("Comando", cmd_default)
        if not cmd_text:
            raise SystemExit("Configuração cancelada: comando CLI vazio.")
        return CliConnection(
            cmd=shlex.split(cmd_text),
            prompt_as_arg=_prompt_bool("Enviar prompt como argumento", cli_defaults.prompt_as_arg),
            output_format=cli_defaults.output_format,
        )

    api_defaults = current if isinstance(current, OpenAIConnection) else OpenAIConnection(
        model=plugin.model or "gpt-4o",
        base_url=plugin.base_url or "https://api.openai.com/v1",
        api_key_env=plugin.api_key_env or "OPENAI_API_KEY",
        provider=plugin.driver if plugin.driver != "cli" else "openai_compat",
        supports_native_tools=plugin.supports_tools,
        extra_body=getattr(current, "extra_body", None),
    )
    provider_default = api_defaults.provider if api_defaults.provider != "openai" else "openai_compat"
    extra_body_raw = _prompt_text("extra_body (JSON, enter para ignorar)", "").strip()
    extra_body = None
    if extra_body_raw:
        try:
            extra_body = json.loads(extra_body_raw)
            # Se o JSON for vazio ({}), trata como "limpar extra_body"
            if extra_body == {}:
                extra_body = None
        except json.JSONDecodeError as exc:
            print(f"JSON inválido: {exc}. extra_body será ignorado.")
            extra_body = api_defaults.extra_body
    else:
        # Enter vazio = preserva o valor anterior
        extra_body = api_defaults.extra_body
    conn = OpenAIConnection(
        model=_prompt_text("Modelo", api_defaults.model) or api_defaults.model,
        base_url=_prompt_text("Base URL", api_defaults.base_url) or api_defaults.base_url,
        api_key_env=_prompt_text("Variável da API key", api_defaults.api_key_env) or api_defaults.api_key_env,
        provider=provider_default,
        supports_native_tools=api_defaults.supports_native_tools,
        extra_body=extra_body,
    )
    return conn


def _parse_extra_body_arg(raw: str | None) -> dict | None:
    """Converte --extra-body de string JSON para dict, com mensagem de erro."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--extra-body: JSON inválido: {exc}") from exc


def _build_connection_from_args(plugin, args):
    """Monta conexão a partir das flags; se estiver incompleta, cai no modo interativo."""
    base_name = getattr(args, "base", None)
    if base_name and args.model:
        base_plugin = _plugins.get(base_name.strip().lower())
        if base_plugin is None:
            raise SystemExit(f"Plugin base '{base_name}' não encontrado.")
        try:
            return base_plugin.configure_with_model(args.model)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if args.driver is None:
        return _configure_connection_interactively(plugin)
    if args.driver == "cli":
        if args.cmd:
            return CliConnection(
                cmd=list(args.cmd),
                prompt_as_arg=False,
                output_format=None,
            )
        return _configure_connection_interactively(plugin, driver_hint="cli")
    if args.model:
        extra_body = _parse_extra_body_arg(getattr(args, "extra_body", None))
        return OpenAIConnection(
            model=args.model,
            base_url=args.base_url or plugin.effective_base_url() or "https://api.openai.com/v1",
            api_key_env=args.api_key_env or plugin.effective_api_key_env() or "OPENAI_API_KEY",
            provider=plugin.effective_driver() if plugin.effective_driver() != "cli" else "openai_compat",
            supports_native_tools=plugin.supports_tools,
            extra_body=extra_body,
        )
    return _configure_connection_interactively(plugin, driver_hint="openai")


def main():
    """Executa main."""
    if hasattr(sys.stdin, "reconfigure"):
        try:
            stdin_encoding = None
            if hasattr(sys.stdin, "fileno"):
                stdin_encoding = os.device_encoding(sys.stdin.fileno())
            stdin_encoding = stdin_encoding or sys.stdin.encoding or locale.getpreferredencoding(False) or "utf-8"
            sys.stdin.reconfigure(encoding=stdin_encoding, errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    parser = argparse.ArgumentParser(prog="quimera")
    parser.add_argument("--name", metavar="NOME", nargs="+", default=None)
    parser.add_argument("--whoami", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--history-window", type=int, default=None)
    parser.add_argument("--visibility", choices=[v.value for v in Visibility], default=Visibility.SUMMARY.value,
                        help="Nível de visibilidade da execução do agente: quiet (stderr truncado), "
                             "summary (início+fim), full (stdout+stderr completos). Padrão: summary")
    parser.add_argument(
        "--agents",
        metavar="AGENTE",
        nargs="+",
        default=["*"],
        help="Lista de agentes (ex: --agents claude gemini). O primeiro é o agente padrão.",
    )
    parser.add_argument("--threads", type=int, default=1, help="Máximo de agentes processados em paralelo por rodada")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout em segundos para execução de agentes")
    parser.add_argument("--idle-timeout", dest="idle_timeout", type=int, default=120,
                        help="Idle timeout em segundos.")
    parser.add_argument("--interactive-test", action="store_true",
                        help="Modo de teste interativo para testes automatizados")
    parser.add_argument("test_agent", nargs="?", default=None,
                        help="Agente para modo de teste (usado com --interactive-test)")
    parser.add_argument("--test-prompt", dest="test_prompt", nargs=argparse.REMAINDER, default=None,
                        help="Prompt para modo de teste")
    parser.add_argument(
        "--theme",
        metavar="TEMA",
        default=None,
        choices=_themes.names(),
        help=f"Tema de exibição para esta sessão. Disponíveis: {', '.join(_themes.names())}",
    )
    parser.add_argument(
        "--set-theme",
        dest="set_theme",
        metavar="TEMA",
        default=None,
        choices=_themes.names(),
        help="Define o tema padrão persistente e encerra.",
    )
    parser.add_argument("--driver-repl", dest="driver_repl", metavar="PLUGIN",
                        default=None,
                        help="Inicia REPL interativo para testar um plugin openai_compat (ex: ollama-qwen)")
    parser.add_argument("--working-dir", dest="working_dir", metavar="DIR", default=None,
                        help="Diretório de trabalho para o REPL (padrão: cwd)")
    parser.add_argument("--prompt", dest="repl_prompt", metavar="TEXTO", default=None,
                        help="Prompt one-shot para --driver-repl (não-interativo, útil para scripts)")
    parser.add_argument("--connect", dest="connect", metavar="AGENTE", default=None,
                        help="Configura interativamente a conexão de um agente e persiste no base_dir")
    parser.add_argument("--base", dest="base", metavar="PLUGIN", default=None,
                        help="Plugin base para herdar cmd/output_format (ex: opencode-pickle)")
    parser.add_argument("--driver", dest="driver", choices=["cli", "openai"], default=None,
                        help="Driver de conexão (cli ou openai)")
    parser.add_argument("--cmd", dest="cmd", metavar="CMD", nargs=argparse.REMAINDER, default=None,
                        help="Comando CLI (para driver=cli)")
    parser.add_argument("--model", dest="model", metavar="MODELO", default=None,
                        help="Modelo (para driver=openai)")
    parser.add_argument("--base-url", dest="base_url", metavar="URL", default=None,
                        help="Base URL (para driver=openai)")
    parser.add_argument("--api-key-env", dest="api_key_env", metavar="VAR", default=None,
                        help="Variável de ambiente com API key (para driver=openai)")
    parser.add_argument("--extra-body", dest="extra_body", metavar="JSON", default=None,
                        help="JSON com parâmetros extras para o corpo da requisição (ex: '{\"thinking\":{\"type\":\"disabled\"}}')")
    parser.add_argument("--list-connections", dest="list_connections", action="store_true",
                        help="Lista conexões persistidas")

    args, unknown = parser.parse_known_args()

    if "--spy" in unknown:
        parser.error("--spy foi removido; use --visibility quiet|summary|full")

    if args.history_window is not None and args.history_window <= 0:
        parser.error("--history-window deve ser maior que zero")

    if args.list_connections:
        conns = load_connections()
        if not conns:
            print("Nenhuma conexão persistida.")
        else:
            for name, data in conns.items():
                print(f"{name}: {format_connection_label(_connection_from_dict(data))}")
        return

    if args.connect:
        agent_name = args.connect.strip().lower()
        plugin = _plugins.get(agent_name)
        if plugin is None:
            if not is_valid_agent_name(agent_name):
                parser.error(f"Nome de agente inválido em --connect: {agent_name}")
            base_name = getattr(args, "base", None)
            base_metadata = None
            if base_name:
                base_plugin = _plugins.get(base_name.strip().lower())
                if base_plugin is None:
                    parser.error(f"Plugin base '{base_name}' não encontrado em --base.")
                base_metadata = {"base": base_plugin.name}
            plugin = register_dynamic_plugin(agent_name, metadata=base_metadata)
            print(f"Agente registrado dinamicamente: {agent_name}")
        else:
            # Plugin já existe — atualiza referência de base se --base foi fornecido
            base_name = getattr(args, "base", None)
            if base_name:
                base_plugin = _plugins.get(base_name.strip().lower())
                if base_plugin is None:
                    parser.error(f"Plugin base '{base_name}' não encontrado em --base.")
                object.__setattr__(plugin, "_base_plugin_name", base_plugin.name)
                if base_plugin.spy_stdout_formatter is not None:
                    plugin.spy_stdout_formatter = base_plugin.spy_stdout_formatter
                if base_plugin.runtime_rw_paths:
                    plugin.runtime_rw_paths = list(base_plugin.runtime_rw_paths)

        print(f"Configurando conexão para {agent_name}")
        print(f"Built-in atual: {format_connection_label(plugin.effective_connection())}")
        connection = _build_connection_from_args(plugin, args)
        set_connection_override(agent_name, connection, persist=True)
        print(f"Conexão salva em base_dir para {agent_name}: {format_connection_label(connection)}")
        return

    if args.driver_repl:
        working_dir = Path(args.working_dir).resolve() if args.working_dir else None
        try:
            repl = DriverRepl(
                args.driver_repl,
                working_dir=working_dir,
                get_plugin=_plugins.get,
                all_plugins=_plugins.all_plugins,
            )
            repl.run(one_shot_prompt=args.repl_prompt)
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
        return

    agents_available = _plugins.all_names()
    agents = _expand_patterns(args.agents, agents_available)
    agents_unknown = [a for a in agents if a not in agents_available]
    if agents_unknown:
        parser.error(
            f"Agente(s) desconhecido(s): {', '.join(agents_unknown)}. Disponíveis: {', '.join(agents_available)}")

    cwd = Path.cwd()
    workspace = Workspace(cwd)
    config = ConfigManager(workspace.config_file)

    if args.name is not None:
        config.set_user_name(" ".join(args.name).strip())
        print(f"Nome configurado: {config.user_name}")
        return

    if args.whoami:
        print(config.user_name)
        return

    if args.set_theme is not None:
        config.set_theme(args.set_theme)
        t = _themes.get(args.set_theme)
        print(f"Tema padrão definido: {t.name} — {t.description}")
        return

    visibility = Visibility(args.visibility)
    app = QuimeraApp(cwd,
                     debug=args.debug,
                     history_window=args.history_window,
                     agents=agents, threads=args.threads,
                     timeout=args.timeout,
                     idle_timeout_seconds=args.idle_timeout,
                     workspace=workspace,
                     visibility=visibility,
                     theme=args.theme)

    if args.interactive_test:
        if TerminalRenderer is None or AgentClient is None:
            raise RuntimeError("Modo interativo não disponível nesta versão")

        default_agent = agents[0] if args.agents != ["*"] and agents else "claude"
        default_prompt = "Use uma ferramenta de shell para executar o comando `pwd` e me diga o diretório atual. Se a ferramenta pedir aprovação, mostre o prompt normalmente."

        if args.test_agent:
            agent_name = args.test_agent
        else:
            agent_name = default_agent

        if args.test_prompt:
            prompt = " ".join(args.test_prompt)
        else:
            prompt = default_prompt

        renderer = TerminalRenderer()
        client = AgentClient(renderer)
        try:
            result = client.call(agent_name, prompt)
        finally:
            client.close()

        renderer.show_system(prompt)
        renderer.show_plain("\n--- RESULTADO LIMPO ---\n")
        renderer.show_plain(result)
        return

    app.run()
