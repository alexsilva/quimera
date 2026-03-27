import argparse
from pathlib import Path

from .app import QuimeraApp
from .config import ConfigManager


def main():
    parser = argparse.ArgumentParser(prog="quimera", add_help=False)
    parser.add_argument("--name", metavar="NOME", nargs="+", default=None)
    parser.add_argument("--whoami", action="store_true")
    args, _ = parser.parse_known_args()

    config = ConfigManager()

    if args.name is not None:
        config.set_user_name(" ".join(args.name).strip())
        print(f"Nome configurado: {config.user_name}")
        return

    if args.whoami:
        print(config.user_name)
        return

    app = QuimeraApp(Path.cwd())
    app.run()
