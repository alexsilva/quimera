"""Componentes de `quimera.app.protocol`."""
import json
import re

from ..constants import EXTEND_MARKER, NEEDS_INPUT_MARKER, STATE_UPDATE_START
from ..shared_state import (
    is_agent_state_key,
    normalize_state_key,
    stamp_state_keys,
    validate_agent_state_value,
)
from . import logger



class AppProtocol:
    """Encapsula parsing de respostas e updates de estado."""

    STATE_UPDATE_PATTERN = re.compile(r"\[STATE_UPDATE\](.*?)\[/STATE_UPDATE\]", re.DOTALL)
    ACK_PATTERN = re.compile(r"^\s*\[ACK:([A-Za-z0-9]+)\]\s*", re.M)

    def __init__(
        self,
        lock,
        shared_state,
        workspace=None,
        decisions_log_path=None,
        turn_stamps=None,
    ) -> None:
        """Inicializa uma instância de AppProtocol."""
        self._lock = lock
        self._shared_state = shared_state
        self._workspace = workspace
        self._decisions_log_path = decisions_log_path
        self._decisions_logger = None
        self._turn_stamps = turn_stamps if turn_stamps is not None else {}

    def _get_decisions_logger(self):
        """Executa lazy load do DecisionsLogger."""
        if self._decisions_logger is not None:
            return self._decisions_logger
        if self._decisions_log_path is None:
            return None
        from ..workspace import DecisionsLogger

        self._decisions_logger = DecisionsLogger(self._decisions_log_path)
        return self._decisions_logger

    _MAX_LIST_LENGTH = 50

    @staticmethod
    def merge_state_value(current, incoming):
        """Mescla state value."""
        if incoming is None:
            if not isinstance(current, list) or len(current) <= AppProtocol._MAX_LIST_LENGTH:
                return current
            return current[-AppProtocol._MAX_LIST_LENGTH :]
        if incoming == "":
            return None
        if isinstance(current, list) and isinstance(incoming, list):
            merged = current.copy()
            for item in incoming:
                if item not in merged:
                    merged.append(item)
            if len(merged) > AppProtocol._MAX_LIST_LENGTH:
                merged = merged[-AppProtocol._MAX_LIST_LENGTH :]
            return merged
        return incoming

    def apply_state_update(self, block_content):
        """Executa apply state update."""
        try:
            payload = json.loads(block_content.strip())
        except json.JSONDecodeError:
            return False

        if not isinstance(payload, dict):
            return False

        with self._lock:
            stamped_keys = set()
            for key, value in payload.items():
                normalized_key = normalize_state_key(key)
                if not normalized_key:
                    continue
                if not is_agent_state_key(normalized_key):
                    logger.warning("[STATE_UPDATE] Ignored unsupported shared_state key: %s", normalized_key)
                    continue
                if not validate_agent_state_value(normalized_key, value):
                    logger.warning(
                        "[STATE_UPDATE] Ignored invalid value for shared_state key %s: %r",
                        normalized_key,
                        type(value).__name__,
                    )
                    continue
                current = self._shared_state.get(normalized_key)
                merged = self.merge_state_value(current, value)
                if merged is None:
                    self._shared_state.pop(normalized_key, None)
                    self._turn_stamps.pop(normalized_key, None)
                else:
                    self._shared_state[normalized_key] = merged
                    stamped_keys.add(normalized_key)
                    if normalized_key == "decisions" and isinstance(value, list):
                        dlogger = self._get_decisions_logger()
                        if dlogger:
                            for item in value:
                                dlogger.append(
                                    item,
                                    {"workspace": str(self._workspace.cwd) if self._workspace else None},
                                )
            if stamped_keys:
                current_turn = self._shared_state.get("_current_turn", 0)
                stamp_state_keys(self._turn_stamps, stamped_keys, current_turn)
        return True

    def parse_response(self, response, **_kwargs):
        """Extrai marcadores de controle e retorna estado estruturado."""
        if response is None:
            return None, None, None, False, False, None

        ack_id = None

        if STATE_UPDATE_START in response:
            for state_match in self.STATE_UPDATE_PATTERN.finditer(response):
                self.apply_state_update(state_match.group(1))
            response = self.STATE_UPDATE_PATTERN.sub("", response).strip()

        ack_match = self.ACK_PATTERN.search(response)
        if ack_match:
            ack_id = ack_match.group(1)
            response = self.ACK_PATTERN.sub("", response, count=1).strip()
            logger.info("[ACK] received ack_id=%s", ack_id)

        if response is None:
            return None, None, None, False, False, ack_id

        extend = response.rstrip().endswith(EXTEND_MARKER)
        if extend:
            response = response.rstrip()[: -len(EXTEND_MARKER)].rstrip()

        needs_human_input = NEEDS_INPUT_MARKER in response
        if needs_human_input:
            response = response.replace(NEEDS_INPUT_MARKER, "").strip()

        return response, None, None, extend, needs_human_input, ack_id
