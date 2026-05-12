"""Utilitários de formatação e truncamento de payload/task.

Extraídos de ``AppTaskServices`` para reduzir acoplamento.
"""
from __future__ import annotations


_MAX_COMPLETED_TASK_RESULTS_CHARS = 2000
_MAX_COMPLETED_TASK_RESULT_DESC_CHARS = 80
_MAX_COMPLETED_TASK_RESULT_VALUE_CHARS = 200


def truncate_tool_result(content: str, max_lines: int = 10) -> str:
    """Trunca tool result para evitar que respostas gigantes poluam o prompt."""
    if not content:
        return content
    lines = content.split("\n")
    if len(lines) <= max_lines:
        return content
    truncated = lines[:max_lines]
    truncated.append(f"... ({len(lines) - max_lines} linhas truncadas)")
    return "\n".join(truncated)


def truncate_payload(payload, max_lines: int = 10):
    """Trunca campos do payload de tool result para evitar estouro de contexto."""
    if not payload:
        return payload
    if not isinstance(payload, dict):
        return payload

    truncated = payload.copy()
    if isinstance(truncated.get("content"), str):
        truncated["content"] = truncate_tool_result(truncated["content"], max_lines)
    if isinstance(truncated.get("error"), str):
        truncated["error"] = truncate_tool_result(truncated["error"], max_lines)
    if isinstance(truncated.get("data"), dict):
        data = truncated["data"].copy()
        for key, value in data.items():
            if isinstance(value, str):
                data[key] = truncate_tool_result(value, max_lines)
        truncated["data"] = data
    return truncated


def format_completed_task_result(
    task: dict,
    *,
    max_desc_chars: int = _MAX_COMPLETED_TASK_RESULT_DESC_CHARS,
    max_value_chars: int = _MAX_COMPLETED_TASK_RESULT_VALUE_CHARS,
) -> str:
    """Formata uma linha compacta para o resumo de tasks concluídas."""
    desc = str(task.get("description", "") or "")[:max_desc_chars]
    result = task.get("result", "")
    if result:
        return f"[task {task['id']}] {desc}: {str(result)[:max_value_chars]}"
    return f"[task {task['id']}] {desc}: concluído"


def build_completed_task_results(
    completed_tasks: list[dict],
    *,
    max_chars: int = _MAX_COMPLETED_TASK_RESULTS_CHARS,
    max_desc_chars: int = _MAX_COMPLETED_TASK_RESULT_DESC_CHARS,
    max_value_chars: int = _MAX_COMPLETED_TASK_RESULT_VALUE_CHARS,
) -> str:
    """Resume tasks concluídas com orçamento global fixo.

    Aplica o orçamento ``_MAX_COMPLETED_TASK_RESULTS_CHARS`` e omite
    tasks mais antigas quando estoura, sinalizando quantas foram omitidas.
    """
    if not completed_tasks:
        return ""

    kept_lines: list[str] = []
    total_chars = 0
    omitted_count = 0

    for task in reversed(completed_tasks):
        line = format_completed_task_result(
            task,
            max_desc_chars=max_desc_chars,
            max_value_chars=max_value_chars,
        )
        separator = 1 if kept_lines else 0
        if total_chars + separator + len(line) > max_chars:
            omitted_count += 1
            continue
        kept_lines.append(line)
        total_chars += separator + len(line)

    if not kept_lines:
        return format_completed_task_result(
            completed_tasks[-1],
            max_desc_chars=max_desc_chars,
            max_value_chars=max_value_chars,
        )[:max_chars]

    kept_lines.reverse()
    if omitted_count:
        omitted_line = f"... ({omitted_count} task(s) concluída(s) anterior(es) omitida(s))"
        candidate = "\n".join([omitted_line, *kept_lines])
        if len(candidate) <= max_chars:
            return candidate
        while kept_lines and len("\n".join([omitted_line, *kept_lines])) > max_chars:
            kept_lines.pop(0)
        if not kept_lines:
            return omitted_line[:max_chars]
        candidate = "\n".join([omitted_line, *kept_lines])
        if len(candidate) > max_chars:
            available_chars = max_chars - len(omitted_line) - 1
            if available_chars <= 0:
                return format_completed_task_result(
                    completed_tasks[-1],
                    max_desc_chars=max_desc_chars,
                    max_value_chars=max_value_chars,
                )[:max_chars]
            kept_lines[0] = kept_lines[0][:available_chars]
        return "\n".join([omitted_line, *kept_lines])
    return "\n".join(kept_lines)
