"""Carrega templates fixos do prompt a partir de `prompt.md`."""

from pathlib import Path

PROMPT_FULL = "FULL"
PROMPT_BASE_RULES = "BASE_RULES"
PROMPT_GOAL_EXECUTION_RULES = "GOAL_EXECUTION_RULES"
PROMPT_REVIEWER_RULE = "REVIEWER_RULE"
PROMPT_SHARED_STATE = "SHARED_STATE"
PROMPT_STATE_UPDATE_RULE = "STATE_UPDATE_RULE"
PROMPT_GOAL_LOCK = "GOAL_LOCK"
PROMPT_STEP_LOCK = "STEP_LOCK"
PROMPT_ACCEPTANCE_CRITERIA = "ACCEPTANCE_CRITERIA"
PROMPT_SCOPE_CONTROL = "SCOPE_CONTROL"
PROMPT_REQUEST = "REQUEST"
PROMPT_FACTS = "FACTS"
PROMPT_DEBATE_RULE = "DEBATE_RULE"
PROMPT_HANDOFF_RULE = "HANDOFF_RULE"
PROMPT_TOOL_RULE = "TOOL_RULE"

_COMMENT_SECTIONS = (
    PROMPT_BASE_RULES,
    PROMPT_GOAL_EXECUTION_RULES,
    PROMPT_REVIEWER_RULE,
    PROMPT_STATE_UPDATE_RULE,
    PROMPT_HANDOFF_RULE,
    PROMPT_TOOL_RULE,
    PROMPT_DEBATE_RULE,
    PROMPT_SHARED_STATE,
    PROMPT_GOAL_LOCK,
    PROMPT_STEP_LOCK,
    PROMPT_ACCEPTANCE_CRITERIA,
    PROMPT_SCOPE_CONTROL,
    PROMPT_REQUEST,
    PROMPT_FACTS,
)


class PromptParser:
    """Parseia seções de um arquivo de template de prompt."""

    def __init__(self, path: Path):
        self._source = path
        self._text = path.read_text(encoding="utf-8").strip()

    def extract_named_tag(self, tag: str) -> str | None:
        text = self._text
        opening_tag = f"<{tag}>"
        closing_tag = f"</{tag}>"
        start = text.find(opening_tag)
        if start == -1:
            return None
        content_start = start + len(opening_tag)
        end = text.find(closing_tag, content_start)
        if end == -1:
            raise ValueError(f"{self._source.name} contém tag não fechada: <{tag}>")
        return text[content_start:end].strip()

    def extract_comment_block(self, name: str) -> str | None:
        text = self._text
        opening_marker = f"<!-- {name}:START -->"
        closing_marker = f"<!-- {name}:END -->"
        start = text.find(opening_marker)
        if start == -1:
            return None
        content_start = start + len(opening_marker)
        end = text.find(closing_marker, content_start)
        if end == -1:
            raise ValueError(f"{self._source.name} contém marcador não fechado: {name}")
        return text[content_start:end].strip()

    def load(self, named_tags: tuple[str, ...] = (), comment_sections: tuple[str, ...] = ()) -> dict[str, str]:
        sections: dict[str, str] = {}
        for tag in named_tags:
            content = self.extract_named_tag(tag)
            if content is not None:
                sections[tag.upper()] = content
        for name in comment_sections:
            content = self.extract_comment_block(name)
            if content is not None:
                sections[name] = content
        required = {t.upper() for t in named_tags} | set(comment_sections)
        missing = sorted(required - sections.keys())
        if missing:
            raise ValueError(f"{self._source.name} está incompleto: {', '.join(missing)}")
        return sections


class PromptTemplate:
    """Centraliza todos os textos fixos do prompt."""

    def __init__(self, path: Path):
        self._path = path
        self._sections: dict[str, str] | None = None

    def _load_sections(self) -> dict[str, str]:
        if self._sections is None:
            self._sections = PromptParser(self._path).load(
                named_tags=("full",),
                comment_sections=_COMMENT_SECTIONS,
            )
        return self._sections

    def _get_section(self, name: str) -> str:
        return self._load_sections()[name]

    @property
    def full(self) -> str:
        return self._get_section(PROMPT_FULL)

    @property
    def base_rules(self) -> str:
        return self._get_section(PROMPT_BASE_RULES)

    @property
    def goal_execution_rules(self) -> str:
        return self._get_section(PROMPT_GOAL_EXECUTION_RULES)

    @property
    def reviewer_rule(self) -> str:
        return self._get_section(PROMPT_REVIEWER_RULE)

    @property
    def state_update_rule(self) -> str:
        return self._get_section(PROMPT_STATE_UPDATE_RULE)

    @property
    def handoff_rule(self) -> str:
        return self._get_section(PROMPT_HANDOFF_RULE)

    @property
    def tool_rule(self) -> str:
        return self._get_section(PROMPT_TOOL_RULE)

    @property
    def debate_rule(self) -> str:
        return self._get_section(PROMPT_DEBATE_RULE)

    @property
    def shared_state(self) -> str:
        return self._get_section(PROMPT_SHARED_STATE)

    @property
    def goal_lock(self) -> str:
        return self._get_section(PROMPT_GOAL_LOCK)

    @property
    def step_lock(self) -> str:
        return self._get_section(PROMPT_STEP_LOCK)

    @property
    def acceptance_criteria(self) -> str:
        return self._get_section(PROMPT_ACCEPTANCE_CRITERIA)

    @property
    def scope_control(self) -> str:
        return self._get_section(PROMPT_SCOPE_CONTROL)

    @property
    def request(self) -> str:
        return self._get_section(PROMPT_REQUEST)

    @property
    def facts(self) -> str:
        return self._get_section(PROMPT_FACTS)

    def render(self, **context) -> str:
        """Renderiza o prompt final a partir do template único."""
        sections = self._load_sections()
        return sections[PROMPT_FULL].format(
            base_rules=sections[PROMPT_BASE_RULES],
            **context,
        )


prompt_template = PromptTemplate(Path(__file__).with_name("prompt.md"))
