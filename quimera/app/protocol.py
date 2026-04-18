"""Componentes de `quimera.app.protocol`."""
import hashlib
import json
import logging
import re
import time

from ..constants import EXTEND_MARKER, NEEDS_INPUT_MARKER, ROUTE_PREFIX, STATE_UPDATE_START


class AppProtocol:
    """Encapsula parsing de respostas, handoffs e updates de estado."""

    HANDOFF_PAYLOAD_PATTERN = re.compile(
        r"^\s*task:\s*([^\n]+?)\s*(?:(?:\n|\|\s*)context:\s*([^\n]+?))?\s*(?:(?:\n|\|\s*)expected:\s*([^\n]+?))?\s*(?:(?:\n|\|\s*)priority:\s*([^\n]+?))?\s*$",
        re.IGNORECASE,
    )
    STATE_UPDATE_PATTERN = re.compile(
        r"\[STATE_UPDATE\](.*?)\[/STATE_UPDATE\]", re.DOTALL
    )
    ROUTE_PATTERN = re.compile(r"\[ROUTE:([A-Za-z0-9_-]+)\]\s*([\s\S]+)", re.M | re.I)
    ACK_PATTERN = re.compile(r"^\s*\[ACK:([A-Za-z0-9]+)\]\s*", re.M)
    PAYLOAD_FIELD_RE = re.compile(r"^\s*(task|context|expected)\s*:", re.IGNORECASE)

    def __init__(self, logger: logging.Logger, decisions_log_path=None) -> None:
        """Inicializa uma instância de AppProtocol."""
        self.logger = logger
        self._decisions_log_path = decisions_log_path
        self._decisions_logger = None

    def _get_decisions_logger(self):
        """Executa lazy load do DecisionsLogger."""
        if self._decisions_logger is not None:
            return self._decisions_logger
        if self._decisions_log_path is None:
            return None
        from ..workspace import DecisionsLogger
        self._decisions_logger = DecisionsLogger(self._decisions_log_path)
        return self._decisions_logger

    @staticmethod
    def merge_state_value(current, incoming):
        """Mescla state value."""
        if incoming is None:
            return current
        if incoming == "":
            return None
        if isinstance(current, list) and isinstance(incoming, list):
            merged = current.copy()
            for item in incoming:
                if item not in merged:
                    merged.append(item)
            return merged
        return incoming

    def apply_state_update(self, app, block_content):
        """Executa apply state update."""
        try:
            payload = json.loads(block_content.strip())
        except json.JSONDecodeError:
            return False

        if not isinstance(payload, dict):
            return False

        with app._lock:
            for key, value in payload.items():
                normalized_key = str(key).strip().lower().replace(" ", "_")
                if not normalized_key:
                    continue
                current = app.shared_state.get(normalized_key)
                merged = self.merge_state_value(current, value)
                if merged is None:
                    app.shared_state.pop(normalized_key, None)
                else:
                    app.shared_state[normalized_key] = merged
                    if normalized_key == "decisions" and isinstance(value, list):
                        logger = self._get_decisions_logger()
                        if logger:
                            for item in value:
                                logger.append(item, {"workspace": str(app.workspace.cwd) if hasattr(app, "workspace") else None})
        return True

    def strip_payload_residual(self, app, text):
        """Remove trailing non-payload lines from captured ROUTE group."""
        if not text:
            return ""
        self.logger.debug("[ROUTE] raw_payload before strip: %r", text)
        kept = []
        for line in text.splitlines():
            if self.PAYLOAD_FIELD_RE.match(line) or (kept and not line.strip()):
                kept.append(line)
            else:
                break
        result = "\n".join(kept).strip()
        self.logger.debug("[ROUTE] raw_payload after strip: %r", result)
        return result

    @staticmethod
    def generate_handoff_id(task, target, timestamp=None):
        """Generate a deterministic ID for a handoff based on task content and target."""
        ts = timestamp or time.time()
        raw = f"{ts}:{target}:{task}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def parse_handoff_payload(self, app, payload, target=None):
        """Interpreta handoff payload."""
        if not payload:
            return None
        match = self.HANDOFF_PAYLOAD_PATTERN.match(payload.strip())
        if not match:
            self.logger.warning("[HANDOFF] Payload did not match regex: %r", payload)
            return None

        groups = match.groups()
        task, context, expected = (groups[i].strip() if groups[i] else None for i in range(3))
        priority_raw = groups[3].strip() if len(groups) > 3 and groups[3] else None
        priority = priority_raw.lower() if priority_raw else "normal"
        if priority not in ("normal", "urgent", "low"):
            priority = "normal"

        if not task:
            self.logger.warning(
                "[HANDOFF] Missing required field 'task' - got task=%r, context=%r, expected=%r",
                task,
                context,
                expected,
            )
            return None

        handoff_id = self.generate_handoff_id(task, target or "unknown")
        return {
            "task": task,
            "context": context,
            "expected": expected,
            "priority": priority,
            "handoff_id": handoff_id,
            "chain": [],
        }

    def parse_response(self, app, response):
        """Extrai marcadores de controle e retorna estado estruturado."""
        if response is None:
            return None, None, None, False, False, None

        route_target, handoff, ack_id = None, None, None

        if STATE_UPDATE_START in response:
            for state_match in self.STATE_UPDATE_PATTERN.finditer(response):
                self.apply_state_update(app, state_match.group(1))
            response = self.STATE_UPDATE_PATTERN.sub("", response).strip()

        ack_match = self.ACK_PATTERN.search(response)
        if ack_match:
            ack_id = ack_match.group(1)
            response = self.ACK_PATTERN.sub("", response, count=1).strip()
            self.logger.info("[ACK] received ack_id=%s", ack_id)

        if ROUTE_PREFIX in response:
            match = self.ROUTE_PATTERN.search(response)
            if match:
                raw_payload = self.strip_payload_residual(app, match.group(2))
                route_target = match.group(1)
                parsed_handoff = self.parse_handoff_payload(app, raw_payload, target=route_target)
                self.logger.info("[ROUTE] match=%s, target=%s", match.group(0)[:100], route_target)
                if parsed_handoff:
                    handoff = parsed_handoff
                    if hasattr(app, "session_state") and app.session_state:
                        try:
                            app.session_state["handoffs_received"] += 1
                        except KeyError:
                            pass
                else:
                    self.logger.warning(
                        "[ROUTE] handoff parse failed for target=%s, payload: %r",
                        route_target,
                        raw_payload,
                    )
                    if hasattr(app, "session_state") and app.session_state:
                        try:
                            app.session_state["handoff_invalid_count"] = app.session_state.get(
                                "handoff_invalid_count", 0
                            ) + 1
                        except KeyError:
                            pass
                    if hasattr(app, "behavior_metrics") and app.behavior_metrics:
                        app.behavior_metrics.record_handoff_sent(route_target, is_invalid=True)
                    route_target = None
                response = self.ROUTE_PATTERN.sub("", response, count=1).strip() or None

        if response is None:
            return None, None, None, False, False, None

        extend = response.rstrip().endswith(EXTEND_MARKER)
        if extend:
            response = response.rstrip()[: -len(EXTEND_MARKER)].rstrip()

        needs_human_input = NEEDS_INPUT_MARKER in response
        if needs_human_input:
            response = response.replace(NEEDS_INPUT_MARKER, "").strip()

        return response, route_target, handoff, extend, needs_human_input, ack_id
