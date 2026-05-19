"""Formatter para evidências em seção semântica do prompt."""

from collections import OrderedDict

from .models import Evidence


class EvidenceFormatter:
    @staticmethod
    def format(evidences: list[Evidence], max_chars: int = 2000) -> str:
        if not evidences:
            return ""

        file_types = {"file_read", "file_edit"}
        file_evidences = [e for e in evidences if e.type in file_types]
        think_evidences = [e for e in evidences if e.type == "think_summary"]

        file_lines = []
        seen_paths = OrderedDict()
        for e in reversed(file_evidences):
            if e.path not in seen_paths:
                seen_paths[e.path] = e.ts

        for path in reversed(seen_paths):
            file_lines.append(f"- {path}")

        think_lines = []
        for e in think_evidences:
            summary = e.summary[:200] if len(e.summary) > 200 else e.summary
            think_lines.append(f"- {summary}")

        file_section = ""
        if file_lines:
            file_section = "### Arquivos visitados\n" + "\n".join(file_lines) + "\n\n"

        think_section = ""
        if think_lines:
            think_section = "### Pensamentos\n" + "\n".join(think_lines) + "\n"

        title = "Contexto Compartilhado de Evidências"
        result = (
            f'<evidence_context title="{title}">\n'
            "Estas evidências resumem arquivos já inspecionados e raciocínios úteis desta sessão.\n\n"
            f"{file_section}{think_section}"
            "</evidence_context>"
        )

        if len(result) > max_chars:
            half_limit = max_chars // 2
            file_part = file_section
            if len(file_part) > half_limit:
                file_part = file_part[:half_limit] + "\n... (truncado)"
            think_part = think_section
            prefix = (
                f'<evidence_context title="{title}">\n'
                "Estas evidências resumem arquivos já inspecionados e raciocínios úteis desta sessão.\n\n"
            )
            suffix = "</evidence_context>"
            remaining = max_chars - len(prefix) - len(file_part) - len(suffix)
            if len(think_part) > remaining:
                think_part = think_part[:remaining] + "\n... (truncado)"
            result = f"{prefix}{file_part}{think_part}{suffix}"

        return result
