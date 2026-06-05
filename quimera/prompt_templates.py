import re
from dataclasses import dataclass
from pathlib import Path

from .prompt_kinds import PromptKind, coerce_prompt_kind


@dataclass(frozen=True)
class PromptBlock:
    """Bloco nomeado de um prompt renderizado, como ``<current_turn>``."""

    name: str
    opening: str
    content: str
    start: int
    end: int


class PromptParser:
    """Parseia seções de um arquivo de template de prompt."""

    _TRUE_STRINGS = {"1", "true", "yes", "on"}
    _FALSE_STRINGS = {"0", "false", "no", "off", ""}

    IF_PATTERN = re.compile(
        r"<!--\s*(NOT_IF|IF):([A-Za-z_][A-Za-z0-9_]*)\s*-->(.*?)<!--\s*END\1:\2\s*-->",
        re.DOTALL,
    )

    def __init__(self, path: Path):
        self._source = path

    def load(self) -> str:
        return self._source.read_text(encoding="utf-8").strip()

    @staticmethod
    def _find_opening_tag_end(text: str, start: int) -> int:
        """Retorna o fim da linha/tag de abertura, preservando `>` dentro de títulos."""
        newline = text.find("\n", start)
        if newline >= 0:
            return newline + 1
        tag_end = text.find(">", start)
        return tag_end + 1 if tag_end >= 0 else -1

    @classmethod
    def iter_blocks(cls, text: str, name: str | None = None) -> list[PromptBlock]:
        """Lista blocos XML-like do prompt renderizado.

        Os templates usam blocos textuais como ``<current_turn ...>``
        em linha própria. Esta rotina entende essa sintaxe sem depender de regex
        frágil com ``>`` dentro de atributos renderizados (ex.: usuário ``>>>``).
        """
        rendered = str(text or "")
        wanted = str(name).strip() if name else None
        tag_pattern = re.compile(r"(?m)^<(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\b")
        blocks: list[PromptBlock] = []
        for match in tag_pattern.finditer(rendered):
            block_name = match.group("name")
            if wanted is not None and block_name != wanted:
                continue
            opening_end = cls._find_opening_tag_end(rendered, match.start())
            if opening_end < 0:
                continue
            closing = f"</{block_name}>"
            closing_start = rendered.find(closing, opening_end)
            if closing_start < 0:
                continue
            closing_end = closing_start + len(closing)
            blocks.append(PromptBlock(
                name=block_name,
                opening=rendered[match.start():opening_end].strip(),
                content=rendered[opening_end:closing_start].strip(),
                start=match.start(),
                end=closing_end,
            ))
        return blocks

    @classmethod
    def extract_last_block(cls, text: str, name: str) -> tuple[str | None, str]:
        """Extrai o último bloco nomeado e retorna ``(conteúdo, texto_sem_bloco)``."""
        blocks = cls.iter_blocks(text, name=name)
        if not blocks:
            return None, str(text or "")
        block = blocks[-1]
        rendered = str(text or "")
        without = (rendered[:block.start] + rendered[block.end:]).strip()
        return block.content or None, without

    @classmethod
    def _resolve_condition_value(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in cls._TRUE_STRINGS:
                return True
            if normalized in cls._FALSE_STRINGS:
                return False
            return bool(value)
        return bool(value)

    @classmethod
    def resolve_conditionals(cls, template: str, context: dict) -> str:
        def replace(match: re.Match[str]) -> str:
            mode, key, content = match.groups()
            value = cls._resolve_condition_value(context.get(key))
            should_include = value if mode == "IF" else not value
            return content.strip() if should_include else ""

        rendered = template
        while True:
            updated = cls.IF_PATTERN.sub(replace, rendered)
            if updated == rendered:
                return updated
            rendered = updated


class PromptTemplate:
    """Centraliza todos os textos fixos do prompt."""

    def __init__(self, path: Path):
        self._path = path
        self._text: str | None = None

    def _load(self) -> str:
        if self._text is None:
            self._text = PromptParser(self._path).load()
        return self._text

    @staticmethod
    def _safe_format(template: str, **context) -> str:
        class _SafeDict(dict):
            def __missing__(self, key):
                return ""

        return template.format_map(_SafeDict(context))

    def render(self, **context) -> str:
        """Renderiza o prompt final a partir do template único."""
        template = PromptParser.resolve_conditionals(self._load(), context)
        rendered = self._safe_format(template, **context)
        return re.sub(r"\n{3,}", "\n\n", rendered).strip()


_PROMPT_FILE_BY_KIND = {
    PromptKind.CHAT: "prompt.md",
    PromptKind.TASK_EXECUTOR: "task_prompt.md",
    PromptKind.TASK_REVIEWER: "task_reviewer_prompt.md",
}

_template_cache: dict[tuple[str, str], PromptTemplate] = {}


def get_prompt_template(kind: PromptKind | str = PromptKind.CHAT) -> PromptTemplate:
    """Retorna o template do prompt solicitado com fallback seguro para chat."""
    normalized = coerce_prompt_kind(kind)
    filename = _PROMPT_FILE_BY_KIND.get(normalized, _PROMPT_FILE_BY_KIND[PromptKind.CHAT])
    template_path = Path(__file__).with_name(filename)
    cache_key = (normalized.value, str(template_path))
    if template_path.exists():
        return _template_cache.setdefault(cache_key, PromptTemplate(template_path))

    fallback_path = Path(__file__).with_name(_PROMPT_FILE_BY_KIND[PromptKind.CHAT])
    fallback_key = (PromptKind.CHAT.value, str(fallback_path))
    return _template_cache.setdefault(fallback_key, PromptTemplate(fallback_path))


prompt_template = get_prompt_template(PromptKind.CHAT)
