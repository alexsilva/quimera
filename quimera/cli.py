"""Componentes de `quimera.cli`."""
import argparse
import importlib.util
import json
import locale
import os
import shlex
import sys
from pathlib import Path

try:
    from prompt_toolkit.shortcuts import prompt as _pt_prompt
    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False

from .connection_configurator import ConnectionConfigurator
from .constants import Visibility
from . import profiles as _profiles
from .profiles.base import (
    CliConnection,
    OpenAIConnection,
    _connection_from_dict,
    connection_to_dict,
    format_connection_label,
    is_valid_agent_name,
    load_connections,
    register_connection_profile,
    save_connections,
    set_connection,
)
from . import themes as _themes
from .app import QuimeraApp
from .app.prompt_input import InputGate
from .runtime.mcp import start_embedded_mcp
from .config import ConfigManager
from .runtime.drivers.repl import DriverRepl
from .workspace import Workspace
from .prompt_templates import PromptText

try:
    from .ui import TerminalRenderer
    from .agents import AgentClient
except ImportError:
    TerminalRenderer = None
    AgentClient = None


_REQUIRED_RUNTIME_DEPENDENCIES = {
    "openai": "openai",
    "prompt-toolkit": "prompt_toolkit",
    "rich": "rich",
}


def _ensure_required_runtime_dependencies() -> None:
    """Falha cedo quando dependências obrigatórias da instalação base estão ausentes."""
    missing = [
        package
        for package, module_name in _REQUIRED_RUNTIME_DEPENDENCIES.items()
        if importlib.util.find_spec(module_name) is None
    ]
    if missing:
        names = ", ".join(f"'{name}'" for name in missing)
        plural = "s" if len(missing) > 1 else ""
        raise SystemExit(
            f"Instalação incompleta: dependência{plural} obrigatória{plural} {names} não encontrada{plural}. "
            "Reinstale o projeto com: pip install -e ."
        )


def _test_profile_names() -> tuple[str, ...]:
    return tuple(getattr(_profiles, "TEST_PROFILE_NAMES", ()))


def _available_agent_names(test_mode: bool = False) -> list[str]:
    if test_mode and hasattr(_profiles, "enable_test_profiles"):
        _profiles.enable_test_profiles()
    test_names = set(_test_profile_names())
    if test_mode:
        names = _profiles.all_names()
        return [name for name in names if name in test_names]
    return [name for name in load_connections().keys() if name not in test_names]


def _test_mode_uses_fake_openai(agents: list[str] | tuple[str, ...] | None) -> bool:
    selected = {str(agent).strip().lower() for agent in (agents or [])}
    return bool(selected & {"fake-openai", "fake-cli-delegate", "fake-openai-mcp-cli"})


def _start_test_fake_openai_backend() -> object:
    """Sobe backend OpenAI-compatible fake local e aplica override não persistente."""
    import threading
    from http.server import ThreadingHTTPServer

    from .devtools.fake_agents import DEFAULT_HOST, DEFAULT_MODEL, FakeOpenAIHandler

    httpd = ThreadingHTTPServer((DEFAULT_HOST, 0), FakeOpenAIHandler)
    httpd.model = DEFAULT_MODEL  # type: ignore[attr-defined]
    httpd.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="quimera-fake-openai")
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        base_url = f"http://{host}:{port}/v1"
        set_connection(
            "fake-openai",
            OpenAIConnection(
                model=DEFAULT_MODEL,
                base_url=base_url,
                api_key_env="QUIMERA_FAKE_API_KEY",
                provider="openai_compat",
                supports_native_tools=True,
            ),
            persist=False,
        )
    except Exception:
        _stop_test_fake_openai_backend(httpd)
        raise
    return httpd


def _stop_test_fake_openai_backend(httpd: object | None) -> None:
    if httpd is None:
        return
    shutdown = getattr(httpd, "shutdown", None)
    if callable(shutdown):
        shutdown()
    server_close = getattr(httpd, "server_close", None)
    if callable(server_close):
        server_close()

def _expand_patterns(agents: list[str], available: list[str]) -> list[str]:
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


def _read_input(prompt_text: str) -> str:
    """Lê entrada interativa usando prompt_toolkit se for um TTY, senão input()."""
    if sys.stdout.isatty():
        return _pt_prompt(prompt_text).strip()
    return input(prompt_text).strip()


def _prompt_text(label: str, default: str | None = None) -> str:
    """Lê um valor interativo com default opcional."""
    suffix = f" [{default}]" if default not in {None, ""} else ""
    value = _read_input(f"{label}{suffix}: ")
    if value:
        return value
    return default or ""


def _prompt_bool(label: str, default: bool = False) -> bool:
    """Lê um booleano interativo."""
    default_label = "s" if default else "n"
    while True:
        raw = _read_input(f"{label} [s/n] [{default_label}]: ").lower()
        if not raw:
            return default
        if raw in {"s", "sim", "y", "yes"}:
            return True
        if raw in {"n", "nao", "não", "no"}:
            return False
        print("Valor inválido. Use 's' ou 'n'.")


def _configure_connection_interactively(profile, driver_hint: str | None = None):
    """Coleta configuração de conexão de forma interativa."""
    configurator = ConnectionConfigurator(_prompt_text, _prompt_bool, print)
    try:
        return configurator.configure(profile, driver_hint=driver_hint)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


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


def _build_connection_from_args(profile, args):
    """Monta conexão a partir das flags; se estiver incompleta, cai no modo interativo."""
    profile_name = getattr(args, "profile", None)
    if profile_name and args.model:
        profile = _profiles.get(profile_name.strip().lower())
        if profile is None:
            raise SystemExit(f"Perfil de execução '{profile_name}' não encontrado.")
        try:
            return profile.configure_with_model(args.model)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if args.driver is None:
        return _configure_connection_interactively(profile)
    if args.driver == "cli":
        if args.cmd:
            output_resolver = getattr(profile, "effective_output_format", None)
            output_format = output_resolver() if callable(output_resolver) else getattr(profile, "output_format", None)
            return CliConnection(
                cmd=list(args.cmd),
                prompt_as_arg=False,
                output_format=output_format,
            )
        return _configure_connection_interactively(profile, driver_hint="cli")
    if args.model:
        extra_body = _parse_extra_body_arg(getattr(args, "extra_body", None))
        return OpenAIConnection(
            model=args.model,
            base_url=args.base_url or profile.effective_base_url() or "https://api.openai.com/v1",
            api_key_env=args.api_key_env or profile.effective_api_key_env() or "OPENAI_API_KEY",
            provider=profile.effective_driver() if profile.effective_driver() != "cli" else "openai_compat",
            supports_native_tools=profile.supports_tools,
            extra_body=extra_body,
        )
    return _configure_connection_interactively(profile, driver_hint="openai")


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
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Ativa métricas e auditoria de renderização em data/logs/render/",
    )
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
    parser.add_argument("--idle-timeout", dest="idle_timeout", type=int, default=None,
                        help="Idle timeout em segundos (sem stdout do agente). Padrão: valor salvo via --set-idle-timeout ou 180s.")
    parser.add_argument(
        "--set-idle-timeout",
        dest="set_idle_timeout",
        type=int,
        metavar="SEGUNDOS",
        default=None,
        help="Persiste o idle timeout padrão (em segundos) na config e encerra.",
    )
    parser.add_argument("--test", action="store_true",
                        help="Ativa modo de teste: somente profiles fake entram na rodada")
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
    parser.add_argument(
        "--set-history-window",
        type=int,
        metavar="N",
        default=None,
        help="Persiste o history_window na config e encerra.",
    )
    parser.add_argument("--driver-repl", dest="driver_repl", metavar="PERFIL",
                        default=None,
                        help="Inicia REPL interativo para testar um profile openai_compat (ex: ollama-qwen)")
    parser.add_argument("--working-dir", dest="working_dir", metavar="DIR", default=None,
                        help="Diretório de trabalho para o REPL (padrão: cwd)")
    parser.add_argument("--prompt", dest="repl_prompt", metavar="TEXTO", default=None,
                        help="Prompt one-shot para --driver-repl (não-interativo, útil para scripts)")
    parser.add_argument("--connect", dest="connect", metavar="AGENTE", default=None,
                        help="Configura interativamente a conexão de um agente e persiste no base_dir")
    parser.add_argument("--profile", dest="profile", metavar="PERFIL", default=None,
                        help="Perfil de execução para herdar cmd/output_format (ex: opencode)")
    parser.add_argument("--driver", dest="driver", choices=["cli", "openai"], default=None,
                        help="Driver de conexão (cli ou openai)")
    parser.add_argument("--cmd", dest="cmd", metavar="CMD", nargs=argparse.REMAINDER, default=None,
                        help="Comando CLI (para driver=cli)")
    parser.add_argument("--model", dest="model", metavar="MODELO", default=None,
                        help="Modelo (para driver=openai ou profile CLI com suporte a modelo)")
    parser.add_argument("--base-url", dest="base_url", metavar="URL", default=None,
                        help="Base URL (para driver=openai)")
    parser.add_argument("--api-key-env", dest="api_key_env", metavar="VAR", default=None,
                        help="Variável de ambiente com API key (para driver=openai)")
    parser.add_argument("--extra-body", dest="extra_body", metavar="JSON", default=None,
                        help="JSON com parâmetros extras para o corpo da requisição (ex: '{\"thinking\":{\"type\":\"disabled\"}}')")
    parser.add_argument("--list-connections", dest="list_connections", action="store_true",
                        help="Lista conexões persistidas")
    parser.add_argument(
        "--no-mcp",
        dest="no_mcp",
        action="store_true",
        default=False,
        help="Desativa o servidor MCP (por padrão ele é iniciado automaticamente).",
    )
    parser.add_argument(
        "--mcp-socket",
        dest="mcp_socket",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Ativa MCP via socket Unix (padrão) e opcionalmente define o path do socket.",
    )
    parser.add_argument(
        "--mcp",
        dest="mcp_socket",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--mcp-http",
        dest="mcp_http",
        action="store_true",
        default=False,
        help="Habilita MCP HTTP externo adicional em /mcp; agentes locais continuam usando o socket Unix interno.",
    )
    parser.add_argument(
        "--mcp-port",
        dest="mcp_port",
        type=int,
        default=9090,
        help="Porta do servidor MCP HTTP externo quando --mcp-http está ativo (padrão: 9090).",
    )
    parser.add_argument(
        "--mcp-host",
        dest="mcp_host",
        default="127.0.0.1",
        help="Host do servidor MCP HTTP externo quando --mcp-http está ativo (padrão: 127.0.0.1).",
    )
    parser.add_argument(
        "--mcp-token-env",
        dest="mcp_token_env",
        default="QUIMERA_MCP_TOKEN",
        metavar="VAR",
        help="Variável de ambiente com token MCP fixo para clientes HTTP externos (padrão: QUIMERA_MCP_TOKEN). Se ausente, gera token externo aleatório por sessão.",
    )
    parser.add_argument(
        "--mcp-http-allow-tools",
        dest="mcp_http_allow_tools",
        default="read",
        metavar="read-local|read|agent|all|CSV",
        help="Allowlist de tools para MCP HTTP externo: read-local (sem rede), read (padrão), agent, all ou lista CSV de nomes.",
    )

    args, unknown = parser.parse_known_args()

    if "--spy" in unknown:
        parser.error("--spy foi removido; use --visibility quiet|summary|full")

    if args.history_window is not None and args.history_window <= 0:
        parser.error("--history-window deve ser maior que zero")

    agents_available = _available_agent_names(test_mode=args.test)

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
        if agent_name in _test_profile_names():
            parser.error(f"Profile de teste '{agent_name}' não aceita --connect persistente; use --test com configuração local do processo")
        profile = _profiles.get(agent_name)
        if profile is not None and not getattr(profile, "dynamic", False):
            parser.error(
                f"'{agent_name}' é um perfil de execução; escolha um nome de conexão e use --profile {agent_name}."
            )
        if profile is None:
            if not is_valid_agent_name(agent_name):
                parser.error(f"Nome de agente inválido em --connect: {agent_name}")
            profile_name = getattr(args, "profile", None)
            profile_metadata = None
            if profile_name:
                profile = _profiles.get(profile_name.strip().lower())
                if profile is None:
                    parser.error(f"Perfil de execução '{profile_name}' não encontrado em --profile.")
                profile_metadata = {"profile": profile.name}
            profile = register_connection_profile(agent_name, metadata=profile_metadata)
            print(f"Conexão registrada: {agent_name}")
        else:
            # Profile já existe — atualiza referência de base se --profile foi fornecido
            profile_name = getattr(args, "profile", None)
            if profile_name:
                execution_profile = _profiles.get(profile_name.strip().lower())
                if execution_profile is None:
                    parser.error(f"Perfil de execução '{profile_name}' não encontrado em --profile.")
                profile = register_connection_profile(agent_name, metadata={"profile": execution_profile.name})

        print(f"Configurando conexão para {agent_name}")
        print(f"Built-in atual: {format_connection_label(profile.effective_connection())}")
        connection = _build_connection_from_args(profile, args)
        set_connection(agent_name, connection, persist=True)
        print(f"Conexão salva em base_dir para {agent_name}: {format_connection_label(connection)}")
        return

    if args.driver_repl:
        _ensure_required_runtime_dependencies()
        if args.driver_repl in _test_profile_names() and not args.test:
            parser.error(f"Profile de teste '{args.driver_repl}' exige --test")
        fake_openai_backend = None
        if args.test and args.driver_repl == "fake-openai":
            fake_openai_backend = _start_test_fake_openai_backend()
        working_dir = Path(args.working_dir).resolve() if args.working_dir else None
        try:
            repl = DriverRepl(
                args.driver_repl,
                working_dir=working_dir,
                get_profile=_profiles.get,
                all_profiles=_profiles.all_profiles,
                input_gate=InputGate(),
            )
            repl.run(one_shot_prompt=args.repl_prompt)
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
        finally:
            _stop_test_fake_openai_backend(fake_openai_backend)
        return

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

    if args.set_history_window is not None:
        if args.set_history_window <= 0:
            parser.error("--set-history-window deve ser maior que zero")
        config.set_history_window(args.set_history_window)
        print(f"History window definida: {args.set_history_window}")
        return

    if args.set_idle_timeout is not None:
        if args.set_idle_timeout <= 0:
            parser.error("--set-idle-timeout deve ser maior que zero")
        config.set_idle_timeout_seconds(args.set_idle_timeout)
        print(f"Idle timeout padrão definido: {args.set_idle_timeout}s")
        return

    _ensure_required_runtime_dependencies()

    visibility = Visibility(args.visibility)
    fake_openai_backend = None
    if args.test and _test_mode_uses_fake_openai(agents):
        fake_openai_backend = _start_test_fake_openai_backend()
    try:
        app = QuimeraApp(cwd,
                         debug=args.debug,
                         history_window=args.history_window,
                         agents=agents, threads=args.threads,
                         idle_timeout_seconds=args.idle_timeout,
                         workspace=workspace,
                         visibility=visibility,
                         theme=args.theme)
        if args.interactive_test:
            if TerminalRenderer is None or AgentClient is None:
                raise RuntimeError("Modo interativo não disponível: dependências de UI não instaladas.")
            default_agent = agents[0] if args.agents != ["*"] and agents else "claude"
            default_prompt = "Use uma ferramenta de shell para executar o comando `pwd` e me diga o diretório atual. Se a ferramenta pedir aprovação, mostre o prompt normalmente."

            if args.test_agent:
                agent_name = args.test_agent
            else:
                agent_name = default_agent

            if args.test_prompt:
                prompt = PromptText(" ".join(args.test_prompt), strict=False)
            else:
                prompt = PromptText(default_prompt, strict=False)

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

        start_embedded_mcp(
            app,
            workspace,
            enabled=not args.no_mcp,
            transport="socket",
            socket_path=args.mcp_socket,
            http_host=args.mcp_host,
            http_port=args.mcp_port,
            token_env=args.mcp_token_env,
            http_allowed_tools=args.mcp_http_allow_tools,
            external_http_enabled=args.mcp_http,
        )

        import queue as _queue
        import threading as _threading
        from .ui.application import QuimeraApplication
        from .constants import CMD_EXIT

        input_services = getattr(app, "input_services", None)
        set_split_queue = getattr(input_services, "set_split_queue", None)
        if not callable(set_split_queue):
            app.run()
            return

        _split_q: _queue.Queue = _queue.Queue()

        def _submit(text: str) -> None:
            _split_q.put(text)

        _toolbar_resolver = None
        if hasattr(app, "toolbar_coordinator"):
            _toolbar_resolver = app.toolbar_coordinator.build_input_toolbar_context

        _command_resolver = getattr(app, "_available_commands", None)
        _argument_resolver = getattr(app, "_command_argument_resolver", None)

        def _cancel_active_agent() -> bool:
            if not getattr(app, "is_agent_running", False):
                return False
            lifecycle = getattr(app, "chat_lifecycle", None)
            handle_interrupt = getattr(lifecycle, "handle_local_interrupt", None)
            if callable(handle_interrupt):
                handle_interrupt()
                return True
            agent_client = getattr(app, "agent_client", None)
            cancel = getattr(agent_client, "cancel_active_work", None)
            if callable(cancel):
                cancel()
                return True
            return False

        def _inject(text: str) -> bool:
            stdin = getattr(app, "active_agent_stdin", None)
            if stdin is not None:
                try:
                    stdin.write(text + "\n")
                    stdin.flush()
                    return True
                except (OSError, ValueError, AttributeError):
                    pass
            return False

        qapp = QuimeraApplication(
            submit_fn=_submit,
            inject_fn=_inject,
            cancel_agent_fn=_cancel_active_agent,
            theme_cycle_fn=getattr(getattr(app, "toolbar_coordinator", None), "cycle_renderer_theme", None),
            history_file=str(getattr(getattr(app, "input_gate", None), "_history_file", "") or "") or None,
            toolbar_context_resolver=_toolbar_resolver,
            command_resolver=_command_resolver,
            argument_resolver=_argument_resolver,
            user_name=getattr(app, "user_name", None),
        )
        set_split_queue(_split_q)

        if hasattr(app, "renderer") and hasattr(app.renderer, "_compositor"):
            app.renderer._compositor.set_app_sink(qapp)

        if hasattr(app, "input_broker"):
            app.input_broker.set_qapp(qapp)

        if hasattr(app, "toolbar_coordinator"):
            _orig_refresh = app.toolbar_coordinator.refresh

            def _patched_refresh():
                _orig_refresh()
                qapp.invalidate()

            app.toolbar_coordinator.refresh = _patched_refresh

        _chat_thread = _threading.Thread(
            target=app.run, daemon=True, name="quimera-split-chat"
        )
        _chat_thread.start()

        try:
            qapp.run()
        finally:
            _split_q.put(CMD_EXIT)
            _chat_thread.join(timeout=30)
    finally:
        _stop_test_fake_openai_backend(fake_openai_backend)
