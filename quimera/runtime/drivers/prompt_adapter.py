"""Converte prompts estruturados do Quimera para mensagens do driver."""
from __future__ import annotations

import re

from ...clipboard_support import ClipboardManager
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

_CONVERSATION_ENTRY_RE = re.compile(r"(?m)^\[([^\]\n]+)\]:[ \t]?(.*)$")
_HUMAN_NAME_RE = re.compile(r"(?im)^Usu[aá]rio humano:\s*(.+?)\s*$")
_SELF_NAME_RE = re.compile(r"(?im)^Voc[eê] [eé]\s+(.+?)\.?\s*$")
_AGENT_NAMES_RE = re.compile(r"(?im)^Agentes de IA nesta conversa:\s*(.+?)\s*$")
_GENERIC_HUMAN_ROLES = frozenset({"human", "user", "usuário", "usuario"})

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
        content = ClipboardManager().to_openai_content(block.content)
        return _message("user", content)
    return _message("user", block.content)


def _human_role_names(blocks) -> set[str]:
    names = set(_GENERIC_HUMAN_ROLES)
    for block in blocks:
        if block.name != "header":
            continue
        match = _HUMAN_NAME_RE.search(str(block.content or ""))
        if match:
            names.add(match.group(1).strip().casefold())
    return names


def _self_role_names(blocks) -> set[str]:
    """Nomes pelos quais o próprio agente aparece rotulado na conversa."""
    names: set[str] = set()
    for block in blocks:
        if block.name != "header":
            continue
        match = _SELF_NAME_RE.search(str(block.content or ""))
        if match:
            names.add(match.group(1).strip().casefold())
    return names


def _declared_agent_names(blocks) -> set[str]:
    """Extrai agentes declarados no header para distinguir fala de conteúdo."""
    names: set[str] = set()
    for block in blocks:
        if block.name != "header":
            continue
        match = _AGENT_NAMES_RE.search(str(block.content or ""))
        if match:
            names.update(
                name.strip().casefold()
                for name in match.group(1).split(",")
                if name.strip()
            )
    return names


def _split_recent_conversation(
    content: str,
    human_roles: set[str],
    self_names: set[str],
    declared_agent_names: set[str] | None = None,
) -> list[dict]:
    """Reconstrói papéis do bloco canônico ``[PAPEL]: conteúdo``.

    Humanos viram ``user`` e falas do próprio agente viram ``assistant``,
    ambos sem o rótulo. Falas de outros agentes viram ``user`` com o rótulo
    ``[NOME]:`` preservado — sem isso o modelo receberia mensagens alheias no
    papel assistant e as trataria como suas, confundindo identidade em salas
    multiagente. Se o header não identifica o próprio agente, todo não-humano
    vira ``assistant`` (comportamento anterior).
    """
    text = str(content or "").strip()
    if not text or text == "[sem itens residuais na conversa recente]":
        return []
    matches = list(_CONVERSATION_ENTRY_RE.finditer(text))
    if declared_agent_names:
        known_speakers = human_roles | self_names | declared_agent_names
        matches = [
            match for match in matches
            if match.group(1).strip().casefold() in known_speakers
        ]
    if not matches:
        return [{"role": "user", "content": text}]

    messages: list[dict] = []
    leading_content = text[:matches[0].start()].strip()
    if leading_content:
        # Um agente que participou do histórico pode já não estar na lista
        # ativa do header. Preserve sua fala rotulada em vez de descartá-la.
        messages.append({"role": "user", "content": leading_content})
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        first_line = match.group(2)
        continuation = text[match.end():end]
        body = (first_line + continuation).strip()
        if not body:
            continue
        label = match.group(1).strip()
        label_key = label.casefold()
        if label_key in human_roles:
            messages.append({"role": "user", "content": body})
        elif label_key in self_names or not self_names:
            messages.append({"role": "assistant", "content": body})
        else:
            messages.append({"role": "user", "content": f"[{label}]: {body}"})
    return messages


def _build_openai_messages_from_prompt(
    prompt: PromptText,
    prompt_kind: PromptKind | str | None = None,
    *,
    split_recent_conversation: bool = False,
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
    human_roles = _human_role_names(blocks) if split_recent_conversation else set()
    self_names = _self_role_names(blocks) if split_recent_conversation else set()
    declared_agent_names = (
        _declared_agent_names(blocks) if split_recent_conversation else set()
    )

    for block in blocks:
        role = roles.get(block.name)
        if not role:
            continue
        if split_recent_conversation and block.name == "recent_conversation":
            messages.extend(
                _split_recent_conversation(
                    block.content,
                    human_roles,
                    self_names,
                    declared_agent_names,
                )
            )
            continue
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
