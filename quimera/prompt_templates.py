import re
from dataclasses import dataclass
from pathlib import Path

from .prompt_kinds import PromptKind, coerce_prompt_kind


@dataclass(frozen=True)
class PromptBlock:
    """Bloco nomeado de um prompt renderizado, como ``<current_turn>``."""

    name: str
    opening: str
    title: str
    content: str
    start: int
    end: int

    @property
    def size(self) -> int:
        """Número de caracteres no conteúdo do bloco."""
        return len(self.content)


class PromptText(str):
    """Prompt renderizado com estrutura preservada.

    ``str(prompt)`` devolve o texto final com tags na ordem renderizada.
    Iterar o objeto devolve os blocos top-level já parseados.
    """

    def __new__(cls, text: str, kind: PromptKind | str = PromptKind.CHAT, strict: bool = True):
        rendered = str(text or "")
        parser = PromptParser(rendered, strict=strict)
        obj = str.__new__(cls, rendered)
        obj.kind = coerce_prompt_kind(kind)
        obj.parser = parser
        return obj

    def __add__(self, other: object) -> "PromptText":
        return PromptText(str.__add__(self, str(other)), self.kind, strict=False)

    def __radd__(self, other: object) -> "PromptText":
        return PromptText(str(other) + str.__str__(self), self.kind, strict=False)

    def __iter__(self):
        return iter(self.blocks)

    @property
    def blocks(self) -> tuple[PromptBlock, ...]:
        return self.parser.blocks


class PromptParser:
    """Parseia blocos estruturados de um prompt renderizado."""

    _TRUE_STRINGS = {"1", "true", "yes", "on"}
    _FALSE_STRINGS = {"0", "false", "no", "off", ""}

    IF_PATTERN = re.compile(
        r"<!--\s*(NOT_IF|IF):([A-Za-z_][A-Za-z0-9_]*)\s*-->(.*?)<!--\s*END\1:\2\s*-->",
        re.DOTALL,
    )
    TITLE_ATTR_PATTERN = re.compile(r"""\btitle\s*=\s*(?:"([^"]*)"|'([^']*)')""")
    TAG_PATTERN = re.compile(r"(?m)^<(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\b")

    def __init__(self, text: str, strict: bool = False):
        self.text = str(text or "")
        self._strict = bool(strict)
        self._validated_cursor = 0
        self.blocks = tuple(self._parse())
        if self._strict:
            self.validate()

    @staticmethod
    def _find_opening_tag_end(text: str, start: int) -> int:
        """Retorna o fim da linha/tag de abertura, preservando `>` dentro de títulos."""
        newline = text.find("\n", start)
        if newline >= 0:
            return newline + 1
        tag_end = text.find(">", start)
        return tag_end + 1 if tag_end >= 0 else -1

    @classmethod
    def _extract_block_title(cls, opening: str, block_name: str) -> str:
        """Extrai ``title=...`` da tag de abertura de um bloco de prompt."""
        match = cls.TITLE_ATTR_PATTERN.search(str(opening or ""))
        if match:
            return (match.group(1) or match.group(2) or "").strip()
        raise ValueError(f"Bloco de prompt <{block_name}> sem atributo title")

    def _parse(self) -> list[PromptBlock]:
        rendered = self.text
        blocks: list[PromptBlock] = []
        cursor = 0
        while cursor < len(rendered):
            match = self.TAG_PATTERN.search(rendered, cursor)
            if match is None:
                break
            block_name = match.group("name")
            opening_end = self._find_opening_tag_end(rendered, match.start())
            if opening_end < 0:
                cursor = match.end()
                continue
            closing = f"</{block_name}>"
            closing_start = rendered.find(closing, opening_end)
            if closing_start < 0:
                tag_end = rendered.find(">", match.start())
                if tag_end >= 0:
                    inline_opening_end = tag_end + 1
                    inline_closing_start = rendered.find(closing, inline_opening_end)
                    if inline_closing_start >= 0:
                        opening_end = inline_opening_end
                        closing_start = inline_closing_start
            if closing_start < 0:
                cursor = opening_end
                continue
            closing_end = closing_start + len(closing)
            opening = rendered[match.start():opening_end].strip()
            try:
                title = self._extract_block_title(opening, block_name)
            except ValueError:
                if self._strict or "_" in block_name:
                    raise
                cursor = opening_end
                continue
            block = PromptBlock(
                name=block_name,
                opening=opening,
                title=title,
                content=rendered[opening_end:closing_start].strip(),
                start=match.start(),
                end=closing_end,
            )
            if self._strict:
                self.validate(block)
            blocks.append(block)
            cursor = closing_end
        return blocks

    def validate(self, block: PromptBlock | None = None) -> None:
        """Valida um bloco parseado ou finaliza a validação estrutural."""
        if not self.text.strip():
            return
        if block is not None:
            if self.text[self._validated_cursor:block.start].strip():
                raise ValueError("Texto fora de blocos no prompt estruturado")
            self._validated_cursor = block.end
            return
        if self.text[self._validated_cursor:].strip():
            raise ValueError("Texto fora de blocos no prompt estruturado")
        if not self.blocks:
            raise ValueError("Texto fora de blocos no prompt estruturado")

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
            self._text = self._path.read_text(encoding="utf-8").strip()
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

    def render_prompt(self, kind: PromptKind | str, **context) -> PromptText:
        """Renderiza o prompt e preserva kind/blocos para adapters estruturados."""
        return PromptText(self.render(**context), kind)


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
