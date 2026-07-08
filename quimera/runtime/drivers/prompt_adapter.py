"""Converte prompts estruturados do Quimera para mensagens do driver."""
from __future__ import annotations

from ...clipboard_support import build_openai_multimodal_content
from ...prompt_kinds import PromptKind, coerce_prompt_kind
from ...prompt_templates import PromptText

TOOL_SYSTEM_PROMPT = (
    "Use as ferramentas disponíveis quando precisar inspecionar ou modificar arquivos. "
    "Se uma ferramenta retornar erro, ajuste a próxima chamada com base no erro e não repita o mesmo payload inválido; "
    "Não peça ao usuário para executar comandos manualmente se você pode fazer isso diretamente; "
    "Na resposta final, resuma arquivos alterados, evidência de validação e próximo passo; "
)

# Blocos "user" que podem carregar imagem anexada e, portanto, devem ser
# convertidos para conteúdo multimodal quando o destino é OpenAI-compatible.
# Cobre chat (current_turn) e os modos task (delegação/review) — sem isso a
# imagem chegaria apenas como texto no perfil task.
_MULTIMODAL_USER_BLOCKS = frozenset({"current_turn", "task_delegation", "task_review"})

# Mapa direto: PromptKind -> nome do bloco -> role no payload OpenAI-compatible.
# Blocos ausentes no mapa são omitidos intencionalmente.
ROLES_BY_KIND = {
    PromptKind.CHAT: {
        "header": "system",
        "session_state": "system",
        "debug_state": "system",
        "evidence_context": "system",
        "rules": "system",
        "execution_state": "system",
        "execution_mode": "system",
        "persistent_context": "system",
        "recent_conversation": "user",
        "delegation": "user",
        "current_turn": "user",
    },
    PromptKind.TASK_EXECUTOR: {
        "header": "system",
        "session_state": "system",
        "debug_state": "system",
        "task_execution_rules": "system",
        "task_delegation": "user",
    },
    PromptKind.TASK_REVIEWER: {
        "header": "system",
        "session_state": "system",
        "debug_state": "system",
        "task_review_rules": "system",
        "task_review": "user",
    },
}


def _message(role: str, content: str | list[dict], title: str = "") -> dict:
    if isinstance(content, list):
        if title:
            content = [{"type": "text", "text": title}] + content
        return {"role": role, "content": content}
    body = str(content or "").strip()
    title = str(title or "").strip()
    if title:
        body = f"{title}\n\n{body}" if body else title
    return {"role": role, "content": body.strip()}


def _message_from_block(block, role: str) -> dict:
    if role == "system":
        return _message("system", block.content, block.title)
    if block.name in _MULTIMODAL_USER_BLOCKS:
        return _message("user", build_openai_multimodal_content(block.content))
    return _message("user", block.content)


def _build_openai_messages_from_prompt(
    prompt: PromptText,
    prompt_kind: PromptKind | str | None = None,
) -> list[dict]:
    """Converte um PromptText em mensagens de API chat."""
    if not prompt.strip():
        return []
    if not hasattr(prompt, "blocks") or not hasattr(prompt, "kind"):
        return [{"role": "user", "content": str(prompt)}]

    blocks = tuple(prompt.blocks)
    if not blocks:
        return [{"role": "user", "content": str(prompt)}]

    kind = coerce_prompt_kind(prompt_kind if prompt_kind is not None else prompt.kind)
    roles = ROLES_BY_KIND.get(kind)
    if roles is None:
        raise ValueError(f"PromptKind sem mapeamento de roles no adapter: {kind.value}")
    messages: list[dict] = []

    for block in blocks:
        role = roles.get(block.name)
        if role:
            messages.append(_message_from_block(block, role))

    return messages


def _build_tool_system_prompt(
    tool_names: list[str],
    workspace_root: str | None,
    shell_allowlist: list[str] | set[str] | tuple[str, ...] | None = None,
) -> str:
    """Monta o system prompt curto usado no modo com ferramentas."""
    _ = (tool_names, workspace_root, shell_allowlist)
    return TOOL_SYSTEM_PROMPT


def _build_tool_budget_prompt(max_tool_hops: int, remaining_tool_hops: int) -> str:
    """Monta contexto explícito de orçamento de tools para a iteração atual."""
    return (
        "Orçamento de ferramentas desta execução: "
        f"max_tool_hops={max_tool_hops}, remaining_tool_hops={remaining_tool_hops}. "
        "Evite chamadas desnecessárias e finalize quando tiver evidência suficiente."
    )
