import argparse
import locale
import os
import sys
from pathlib import Path
from typing import List

from .app import QuimeraApp
from .config import ConfigManager
from . import plugins as _plugins


def _expand_patterns(agents: List[str], available: List[str]) -> List[str]:
    # Expand patterns like "opencode-*" to all available agents that start with the prefix.
    # Also ensure unique results while preserving the order of first appearance.
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
    parser.add_argument(
        "--agents",
        metavar="AGENTE",
        nargs="+",
        default=["claude"],
        help="Lista de agentes (ex: --agents claude gemini). O primeiro é o agente padrão.",
    )
    parser.add_argument("--threads", type=int, default=1, help="Máximo de agentes processados em paralelo por rodada")
    args, _ = parser.parse_known_args()

    config = ConfigManager()

    if args.name is not None:
        config.set_user_name(" ".join(args.name).strip())
        print(f"Nome configurado: {config.user_name}")
        return

    if args.whoami:
        print(config.user_name)
        return

    if args.history_window is not None and args.history_window <= 0:
        parser.error("--history-window deve ser maior que zero")

    available = _plugins.all_names()
    requested = _expand_patterns(args.agents, available)
    unknown = [a for a in requested if a not in available]
    if unknown:
        parser.error(f"Agente(s) desconhecido(s): {', '.join(unknown)}. Disponíveis: {', '.join(available)}")

    debug = args.debug or os.getenv("QUIMERA_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    app = QuimeraApp(Path.cwd(), debug=debug, history_window=args.history_window, agents=requested, threads=args.threads)
    app.run()
