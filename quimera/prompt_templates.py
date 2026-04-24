import re
from pathlib import Path

class PromptParser:
    """Parseia seções de um arquivo de template de prompt."""

    IF_PATTERN = re.compile(
        r"<!--\s*(NOT_IF|IF):([A-Za-z_][A-Za-z0-9_]*)\s*-->(.*?)<!--\s*END\1:\2\s*-->",
        re.DOTALL,
    )

    def __init__(self, path: Path):
        self._source = path

    def load(self) -> str:
        return self._source.read_text(encoding="utf-8").strip()

    @classmethod
    def resolve_conditionals(cls, template: str, context: dict) -> str:
        def replace(match: re.Match[str]) -> str:
            mode, key, content = match.groups()
            value = bool(context.get(key))
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


prompt_template = PromptTemplate(Path(__file__).with_name("prompt.md"))
