"""Componentes de `quimera.context`."""
import shlex
import unicodedata
from datetime import datetime

from .constants import CMD_CONTEXT
from .editor import Editor
from . import process_factory as subprocess


class ContextManager:
    """Gerencia o contexto persistente carregado no início de cada rodada."""

    SUMMARY_MARKER = "## Resumo da última sessão"
    GENERATED_AT_PREFIX = "_Gerado em "

    def __init__(self, base_context_file, session_context_file, renderer, previous_session_file=None,
                 max_context_lines: int = 2000, workspace=None):
        """Inicializa uma instância de ContextManager."""
        self.base_context_file = base_context_file
        self.session_context_file = session_context_file
        self.renderer = renderer
        self.previous_session_file = previous_session_file
        # Limita o tamanho do contexto para evitar consumo de memória excessivo
        self.max_context_lines = int(max_context_lines) if max_context_lines is not None else 2000
        self.workspace = workspace

    def _base_context_path(self):
        """Resolve o arquivo de contexto persistente atual."""
        if self.workspace is not None:
            return self.workspace.context_persistent
        return self.base_context_file
    
    def handle_context_branch(self, command: str) -> bool:
        """Processa o comando /context branch [branch]."""
        rest = command[len(CMD_CONTEXT):].strip()
        parts = shlex.split(rest) if rest else []
        if parts and parts[0] == "branch":
            parts = parts[1:]
        workspace = self.workspace
        if workspace is None:
            self.renderer.show_warning("Workspace não disponível.")
            return True
        if parts:
            branch = parts[0]
        else:
            try:
                branch = subprocess.read_text(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=str(workspace.cwd),
                    stderr=subprocess.DEVNULL,
                ).strip()
            except subprocess.CalledProcessError:
                self.renderer.show_warning("Não foi possível detectar a branch via git.")
                return True
        workspace.set_branch(branch)
        ctx_path = workspace.context_persistent
        self.renderer.show_system(
            f"Branch definida: {branch}\n"
            f"Contexto persistente: {ctx_path}"
        )
        return True

    def _read(self, path):
        """Lê read."""
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def load_base(self):
        """Carrega base."""
        return self._read(self._base_context_path())

    def load_session(self):
        """Carrega session."""
        return self._read(self.session_context_file)

    def load_previous_session(self):
        """Carrega o resumo da sessão anterior (previous_session.md)."""
        if self.previous_session_file is None:
            return ""
        return self._read(self.previous_session_file)

    def save_previous_session(self, summary):
        """Salva o resumo da sessão como ponto de warm-start para a próxima sessão."""
        if self.previous_session_file is None:
            return
        self.previous_session_file.write_text(summary.strip() + "\n", encoding="utf-8")

    def load_session_summary(self):
        """Extrai apenas o corpo do resumo curado salvo em session.md."""
        session_context = self.load_session()
        if not session_context.startswith(self.SUMMARY_MARKER):
            return ""

        lines = session_context.splitlines()
        if lines and lines[0].strip() == self.SUMMARY_MARKER:
            lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
        if lines and lines[0].startswith(self.GENERATED_AT_PREFIX):
            lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
        return "\n".join(lines).strip()

    @staticmethod
    def _normalize_heading(text):
        """Normaliza headings para filtros simples e previsíveis."""
        normalized = unicodedata.normalize("NFKD", text or "")
        return "".join(char for char in normalized if not unicodedata.combining(char)).lower()

    def _filter_summary_for_prompt(self, summary):
        """Remove seções de pendências para não ancorar o prompt no objetivo antigo."""
        if not summary:
            return ""

        blocked_tokens = ("pendenc", "proximos passos", "proximo passo", "next step", "next steps")
        kept_lines = []
        skipping_section = False

        for line in summary.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                normalized = self._normalize_heading(stripped[3:])
                skipping_section = any(token in normalized for token in blocked_tokens)
                if skipping_section:
                    continue
            if skipping_section:
                continue
            kept_lines.append(line)

        return "\n".join(kept_lines).strip()

    def load(self):
        """Carrega load, incluindo previous_session.md se disponível (warm-start)."""
        base_context = self.load_base()
        session_context = self.load_session()
        session_summary = self._filter_summary_for_prompt(self.load_session_summary())

        parts = []
        if base_context:
            parts.append(base_context)
        if session_summary:
            parts.append(session_summary)
        elif session_context and not session_context.startswith(self.SUMMARY_MARKER):
            parts.append(session_context)

        if parts:
            context = "\n\n".join(parts).strip()
            # Enforce maximum number of lines to prevent unbounded growth
            lines = context.splitlines()
            if self.max_context_lines > 0 and len(lines) > self.max_context_lines:
                context = "\n".join(lines[-self.max_context_lines:])
            return context
        return ""

    def show(self):
        """Exibe show."""
        context = self.load()
        if not context:
            self.renderer.show_system("\n[contexto vazio]\n")
            return
        self.renderer.show_plain(f"\n{context}\n")

    def edit(self):
        """Executa edit."""
        Editor(self.renderer).open_file(self._base_context_path())

    def update_with_summary(self, summary):
        """Substitui ou cria a seção de resumo curado da última sessão em arquivo local."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_section = f"{self.SUMMARY_MARKER}\n\n_Gerado em {timestamp}_\n{summary}"
        self.session_context_file.write_text(new_section.strip() + "\n", encoding="utf-8")
        self.renderer.show_system(f"[memória] resumo salvo em {self.session_context_file.name}")
