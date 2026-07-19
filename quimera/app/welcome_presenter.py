"""Apresentação de boas-vindas: logo ASCII, versão e mensagem inicial."""
from quimera.version import resolve_version


class WelcomePresenter:
    """Apresentação de boas-vindas: logo, versão e mensagem inicial."""

    LOGO = (
        " █████╗ ██╗██╗ ██╗ ███╗███╗ █████╗ █████╗   ████╗\n"
        "██╔═██╗ ██║██║ ██║ ███▄▄██║ ██╔══╝ ██╔═██╗ ██╔═██╗\n"
        "██║▄██║ ██║██║ ██║ ██╔█╔██║ ████╗  █████╔╝ ██████║\n"
        "╚████╔╝ ╚███╔╝ ██║ ██║╚╝██║ █████╗ ██║╚██╗ ██║ ██║\n"
        " ╚═▀▀═╝  ╚══╝  ╚═╝ ╚═╝  ╚═╝ ╚════╝ ╚═╝ ╚═╝ ╚═╝ ╚═╝"
    )

    @staticmethod
    def resolve_app_version() -> str:
        """Resolve a versão instalada pela fonte única da aplicação."""
        return resolve_version()

    @staticmethod
    def build_welcome_logo() -> str:
        """Retorna logo ASCII simples para o banner inicial."""
        return WelcomePresenter.LOGO

    @staticmethod
    def build_welcome_message() -> str:
        """Monta texto de boas-vindas com logo e versão."""
        version = WelcomePresenter.resolve_app_version()
        logo_lines = WelcomePresenter.build_welcome_logo().split("\n")
        width = max(len(line) for line in logo_lines)
        logo_lines.append(f"v{version}".rjust(width))
        return f"{chr(10).join(logo_lines)}\n"
