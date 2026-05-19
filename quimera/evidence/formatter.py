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
        tool_evidences = [e for e in evidences if e.type == "tool_call"]

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

        tool_lines = []
        seen_tool_summaries = OrderedDict()
        for e in reversed(tool_evidences):
            summary = e.summary.strip()
            if summary and summary not in seen_tool_summaries:
                seen_tool_summaries[summary] = e.ts

        for summary in reversed(seen_tool_summaries):
            tool_lines.append(f"- {summary}")

        tool_section = ""
        if tool_lines:
            tool_section = "### Execução recente\n" + "\n".join(tool_lines) + "\n"

        title = "Contexto Compartilhado de Evidências"
        intro = "Estas evidências resumem arquivos já inspecionados, execuções úteis e raciocínios desta sessão."
        result = (
            f'<evidence_context title="{title}">\n'
            f"{intro}\n\n"
            f"{file_section}{tool_section}{think_section}"
            "</evidence_context>"
        )

        if len(result) > max_chars:
            truncation_marker = "\n... (truncado)"
            half_limit = max_chars // 2
            file_part = file_section
            if len(file_part) > half_limit:
                trimmed = max(0, half_limit - len(truncation_marker))
                file_part = file_part[:trimmed] + truncation_marker
            think_part = think_section
            prefix = (
                f'<evidence_context title="{title}">\n'
                f"{intro}\n\n"
            )
            suffix = "</evidence_context>"
            execution_part = tool_section
            remaining = max_chars - len(prefix) - len(file_part) - len(execution_part) - len(suffix)
            if remaining < 0:
                execution_part = execution_part[: max(0, max_chars - len(prefix) - len(file_part) - len(suffix))]
                remaining = 0
            if len(think_part) > remaining:
                if remaining <= len(truncation_marker):
                    think_part = think_part[:remaining]
                else:
                    think_part = think_part[: remaining - len(truncation_marker)] + truncation_marker
            result = f"{prefix}{file_part}{execution_part}{think_part}{suffix}"

        return result
