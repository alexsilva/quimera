import argparse
import os
from pathlib import Path

from .app import QuimeraApp
from .config import ConfigManager


def main():
    parser = argparse.ArgumentParser(prog="quimera")
    parser.add_argument("--name", metavar="NOME", nargs="+", default=None)
    parser.add_argument("--whoami", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args, _ = parser.parse_known_args()

    config = ConfigManager()

    if args.name is not None:
        config.set_user_name(" ".join(args.name).strip())
        print(f"Nome configurado: {config.user_name}")
        return

    if args.whoami:
        print(config.user_name)
        return

    debug = args.debug or os.getenv("QUIMERA_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    app = QuimeraApp(Path.cwd(), debug=debug)
    app.run()
