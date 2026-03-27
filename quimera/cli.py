from pathlib import Path

from .app import QuimeraApp


def main():
    """Inicializa e executa a aplicação a partir do diretório de trabalho atual."""
    app = QuimeraApp(Path.cwd())
    app.run()
