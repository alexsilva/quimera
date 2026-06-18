"""Apresentação de boas-vindas: logo ASCII, versão e mensagem inicial."""
from importlib import metadata


class WelcomePresenter:
    """Apresentação de boas-vindas: logo, versão e mensagem inicial."""

    LOGO = (
        " / __ \\__  __(_)___ ___  ___  _________ _\n"
        "/ / / / / / / / __ `__ \\/ _ \\/ ___/ __ `/\n"
        "/ /_/ / /_/ / / / / / / /  __/ /  / /_/ / \n"
        "\\___\\_\\__,_/_/_/ /_/ /_/\\___/_/   \\__,_/  "
    )

    @staticmethod
    def resolve_app_version() -> str:
        """Resolve a versão instalada do pacote, com fallback seguro."""
        try:
            ver = metadata.version("quimera")
            if ver is not None:
                return ver
        except Exception:
            pass
        return "dev"

    @staticmethod
    def build_welcome_logo() -> str:
        """Retorna logo ASCII simples para o banner inicial."""
        return WelcomePresenter.LOGO

    @staticmethod
    def build_welcome_message() -> str:
        """Monta texto de boas-vindas com logo e versão."""
        version = WelcomePresenter.resolve_app_version()
        logo_lines = WelcomePresenter.build_welcome_logo().split("\n")
        logo_lines[-1] = logo_lines[-1].rstrip() + f"  v{version}"
        return f"{chr(10).join(logo_lines)}\n"
