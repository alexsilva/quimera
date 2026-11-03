"""Utilitários de formatação e truncamento de payload/task.

Extraídos de ``AppTaskServices`` para reduzir acoplamento.
"""
from __future__ import annotations

from ..domain.task_states import TaskStatus
from .events import TaskRequeued


_MAX_COMPLETED_TASK_RESULTS_CHARS = 2000
_MAX_COMPLETED_TASK_RESULT_DESC_CHARS = 80
_MAX_COMPLETED_TASK_RESULT_VALUE_CHARS = 200
_MAX_TASK_FEEDBACK_SUMMARY_CHARS = 140
_MAX_TASK_FEEDBACK_SUMMARY_LINES = 6


def format_completed_task_result(
    task,
    *,
    max_desc_chars: int = _MAX_COMPLETED_TASK_RESULT_DESC_CHARS,
    max_value_chars: int = _MAX_COMPLETED_TASK_RESULT_VALUE_CHARS,
) -> str:
    """Formata uma linha compacta para o resumo de tasks concluídas."""
    desc = str(task.description or "")[:max_desc_chars]
    result = task.result or ""
    if result:
        return f"[task {task.id}] {desc}: {str(result)[:max_value_chars]}"
    return f"[task {task.id}] {desc}: concluído"


def build_completed_task_results(
    completed_tasks: list,
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


def summarize_task_feedback(
    result: str | None,
    *,
    max_chars: int = _MAX_TASK_FEEDBACK_SUMMARY_CHARS,
    max_lines: int = _MAX_TASK_FEEDBACK_SUMMARY_LINES,
) -> str:
    if not result:
        return ""
    text = str(result)
    lines = text.split("\n")
    stripped = _strip_empty_trailing(lines)
    kept = stripped[:max_lines]
    if len(kept) < len(stripped):
        kept.append("…")
    summary = "\n".join(kept)
    if len(summary) <= max_chars:
        return summary
    if max_chars <= 1:
        return summary[:max_chars]
    return f"{summary[: max_chars - 1].rstrip()}…"


def _strip_empty_trailing(lines: list[str]) -> list[str]:
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def format_task_requeue(event: TaskRequeued) -> str:
    """Linha do feed para a task que voltou à fila: quem fica com ela e por quê."""
    attempt = f"tentativa {event.attempt + 1}"
    if event.assigned_to:
        line = f"[task {event.task_id}] volta para {event.assigned_to} refazer ({attempt})"
    elif event.failed_agent:
        line = f"[task {event.task_id}] sai de {event.failed_agent} e passa para outro agente ({attempt})"
    else:
        line = f"[task {event.task_id}] volta para a fila ({attempt})"
    reason = next(
        (part.strip() for part in str(event.reason or "").splitlines() if part.strip()), ""
    )
    if len(reason) > _MAX_TASK_FEEDBACK_SUMMARY_CHARS:
        reason = f"{reason[: _MAX_TASK_FEEDBACK_SUMMARY_CHARS - 1].rstrip()}…"
    return f"{line}: {reason}" if reason else line


def describe_rework(status: TaskStatus | None, owner: str | None) -> str:
    """Desfecho de uma devolução de entrega (``TaskRepository.rework_task``) para o feed."""
    if status == TaskStatus.PENDING:
        return f"volta para {owner} refazer" if owner else "volta para a fila"
    if status == TaskStatus.FAILED:
        return "limite de entregas devolvidas atingido; a task falhou"
    return "a task mudou durante a run; nada foi devolvido"
