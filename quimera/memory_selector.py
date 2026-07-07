from .config import DEFAULT_HISTORY_WINDOW, DEFAULT_USER_NAME
from .constants import EXTEND_MARKER


class MemorySelector:
    """Seleciona contexto relevante do histórico de conversa."""

    def __init__(self, history_window=DEFAULT_HISTORY_WINDOW, user_name=None):
        self.history_window = history_window
        self.user_name = user_name or DEFAULT_USER_NAME

    def select_request(self, history):
        """Retorna (index, content) da última mensagem do usuário na janela."""
        window_start = max(0, len(history) - self.history_window)
        for index in range(len(history) - 1, window_start - 1, -1):
            message = history[index]
            if message.get("role") == "human":
                content = (message.get("content") or "").strip()
                if content:
                    return index, content
        return None, ""

    def find_request_index(self, history, request_content):
        """Retorna o índice mais recente da mensagem humana que casa com o request."""
        target = (request_content or "").strip()
        if not target:
            return None
        window_start = max(0, len(history) - self.history_window)
        for index in range(len(history) - 1, window_start - 1, -1):
            message = history[index]
            if message.get("role") != "human":
                continue
            content = (message.get("content") or "").strip()
            if content == target:
                return index
        return None

    @staticmethod
    def _is_pure_protocol_envelope(content):
        stripped = content.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            return False
        lowered = stripped.lower()
        return '"type"' in lowered and any(marker in lowered for marker in (
            '"type": "delegation"',
            '"type": "state_update"',
            '"type": "ack"',
        ))

    _BLOCKED_SUBSTRINGS = frozenset({
        "goal_canonical",
        "prompt_state",
        "objetivo fixo",
        "não redefina o objetivo",
        "nao redefina o objetivo",
        "fatos observados recentes",
        # Diff markers
        "git diff",
        "diff --git ",
        "```diff",
        "+++ b/",
        "--- a/",
        "arquivo alterado:",
        # Protocol / control markers
        "[ack:",
        EXTEND_MARKER.lower(),
    })

    @staticmethod
    def should_skip_fact(content):
        """Indica se o conteúdo deve ser excluído da área de fatos/contexto."""
        if not (content or "").strip():
            return True
        if MemorySelector._is_pure_protocol_envelope(content):
            return True
        lowered = content.lower()
        return any(marker in lowered for marker in MemorySelector._BLOCKED_SUBSTRINGS)

    def build_conversation_block(self, history, skip_indexes=None, current_agent=None):
        """Compila bloco formatado com mensagens da janela, pulando índices específicos."""
        skip_indexes = skip_indexes or set()
        window_start = max(0, len(history) - self.history_window)
        lines = []
        included_indexes = set()
        for index, message in enumerate(history[window_start:], start=window_start):
            if index in skip_indexes:
                continue
            content = (message.get("content") or "").strip()
            if not content:
                continue
            if message.get("role") != "human" and self.should_skip_fact(content):
                continue
            included_indexes.add(index)
            lines.append(
                self._format_conversation_entry(
                    role=self._display_role(message["role"]),
                    content=content,
                )
            )

        current_agent_lower = (current_agent or "").strip().lower()
        if current_agent_lower:
            for index in range(window_start - 1, -1, -1):
                message = history[index]
                if str(message.get("role") or "").strip().lower() != current_agent_lower:
                    continue
                if index in skip_indexes or index in included_indexes:
                    break
                content = (message.get("content") or "").strip()
                if not content:
                    break
                if self.should_skip_fact(content):
                    continue
                lines.insert(
                    0,
                    self._format_conversation_entry(
                        role=self._display_role(message["role"]),
                        content=content,
                    ),
                )
                break
        return "\n".join(lines) if lines else "[sem itens residuais na conversa recente]"

    @staticmethod
    def _format_conversation_entry(role, content):
        """Padroniza uma linha do bloco de conversa recente."""
        return f"[{role}]: {content}"

    def _display_role(self, role):
        """Normaliza o papel para exibição, preservando o nome do usuário configurado."""
        if role == "human":
            return self.user_name.upper()
        return role.upper()
