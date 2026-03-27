import os
import subprocess
from datetime import datetime


class ContextManager:
    """Gerencia o contexto persistente carregado no início de cada rodada."""

    SUMMARY_MARKER = "## Resumo da última sessão"

    def __init__(self, base_context_file, session_context_file, renderer):
        self.base_context_file = base_context_file
        self.session_context_file = session_context_file
        self.renderer = renderer

    def _read(self, path):
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def load_base(self):
        return self._read(self.base_context_file)

    def load_session(self):
        return self._read(self.session_context_file)

    def load(self):
        base_context = self.load_base()
        session_context = self.load_session()

        if base_context and session_context:
            return f"{base_context}\n\n{session_context}"
        if base_context:
            return base_context
        if session_context:
            return session_context
        return ""

    def show(self):
        context = self.load()
        if not context:
            self.renderer.show_system("\n[contexto vazio]\n")
            return
        self.renderer.show_plain(f"\n{context}\n")

    def edit(self):
        editor = os.environ.get("EDITOR")
        if not editor:
            self.renderer.show_warning("\nDefina a variável EDITOR para usar /context edit.\n")
            return

        try:
            subprocess.run([editor, str(self.base_context_file)], check=True)
        except FileNotFoundError:
            self.renderer.show_error(f"\nEditor não encontrado: {editor}\n")
        except subprocess.CalledProcessError as exc:
            self.renderer.show_error(
                f"\nFalha ao abrir o contexto no editor (código {exc.returncode}).\n"
            )

    def update_with_summary(self, summary):
        """Substitui ou cria a seção de resumo curado da última sessão em arquivo local."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_section = f"{self.SUMMARY_MARKER}\n\n_Gerado em {timestamp}_\n\n{summary}"
        self.session_context_file.write_text(new_section.strip() + "\n", encoding="utf-8")
        self.renderer.show_system(f"[memória] resumo salvo em {self.session_context_file.name}\n")
