"""Componentes de `quimera.cli`."""
import argparse
import locale
import os
import sys
from pathlib import Path
from typing import List

from . import plugins as _plugins
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
    parser.add_argument("--spy", action="store_true",
                        help="Permite ao agente humano inspecionar alterações do agent de IA",
                        default=False)
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

    args, _ = parser.parse_known_args()

    if args.history_window is not None and args.history_window <= 0:
        parser.error("--history-window deve ser maior que zero")

    if args.driver_repl:
        working_dir = Path(args.working_dir).resolve() if args.working_dir else None
        try:
            repl = DriverRepl(args.driver_repl, working_dir=working_dir)
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

    app = QuimeraApp(cwd,
                     debug=args.debug,
                     history_window=args.history_window,
                     agents=agents, threads=args.threads,
                     timeout=args.timeout,
                     idle_timeout_seconds=args.idle_timeout,
                     workspace=workspace,
                     spy=args.spy,
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
        result = client.call(agent_name, prompt)

        renderer.show_system(prompt)
        renderer.show_plain("\n--- RESULTADO LIMPO ---\n")
        renderer.show_plain(result)
        return

    app.run()
