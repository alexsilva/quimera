"""Componentes de `quimera.app.protocol`."""
import hashlib
import json
import re
import time
from dataclasses import dataclass

from ..constants import EXTEND_MARKER, NEEDS_INPUT_MARKER, ROUTE_PREFIX, STATE_UPDATE_START
from . import logger


@dataclass
class ProtocolEnvelope:
    type: str
    content: str
    route: str | None = None
    state_updates: dict | None = None
    metadata: dict | None = None
    handoff_id: str | None = None


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

    def __init__(self, app=None, decisions_log_path=None) -> None:
        """Inicializa uma instância de AppProtocol."""
        self.app = app
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

    _MAX_LIST_LENGTH = 50

    @staticmethod
    def merge_state_value(current, incoming):
        """Mescla state value."""
        if incoming is None:
            return current if not isinstance(current, list) or len(current) <= AppProtocol._MAX_LIST_LENGTH else current[-AppProtocol._MAX_LIST_LENGTH:]
        if incoming == "":
            return None
        if isinstance(current, list) and isinstance(incoming, list):
            merged = current.copy()
            for item in incoming:
                if item not in merged:
                    merged.append(item)
            if len(merged) > AppProtocol._MAX_LIST_LENGTH:
                merged = merged[-AppProtocol._MAX_LIST_LENGTH:]
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

        app = self.app
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
                        dlogger = self._get_decisions_logger()
                        if dlogger:
                            for item in value:
                                dlogger.append(item, {
                                    "workspace": str(app.workspace.cwd) if hasattr(app, "workspace") else None})
        return True

    @staticmethod
    def generate_handoff_id(task, target, timestamp=None):
        """Generate a deterministic ID for a handoff based on task content and target."""
        ts = timestamp or time.time()
        raw = f"{ts}:{target}:{task}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    @staticmethod
    def parse_envelope(text):
        """Tenta parsear como JSON envelope primeiro; retorna None se não for JSON válido."""
        text = text.strip()
        if not (text.startswith("{") and text.endswith("}")):
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or "type" not in data:
            return None
        return ProtocolEnvelope(
            type=str(data["type"]),
            content=str(data.get("content", "")),
            route=str(data["route"]) if data.get("route") else None,
            state_updates=data.get("state_updates"),
            metadata=data.get("metadata"),
            handoff_id=str(data["handoff_id"]) if data.get("handoff_id") else None,
        )

    @classmethod
    def validate_handoff_envelope(cls, envelope):
        """Valida se um ProtocolEnvelope do tipo 'handoff' tem campos obrigatórios.
        Retorna (is_valid, error_message)."""
        if not isinstance(envelope, ProtocolEnvelope):
            return False, "not a ProtocolEnvelope"
        if envelope.type != "handoff":
            return False, f"expected type='handoff', got '{envelope.type}'"
        if not envelope.route:
            return False, "handoff missing 'route' target"
        if not envelope.content or not envelope.content.strip():
            return False, "handoff missing 'content' (task description)"
        return True, ""

    def parse_handoff_payload(self, payload, target=None):
        """Interpreta handoff payload."""
        if not payload:
            return None
        match = self.HANDOFF_PAYLOAD_PATTERN.match(payload.strip())
        if not match:
            logger.warning("[HANDOFF] Payload did not match regex: %r", payload)
            return None

        groups = match.groups()
        task, context, expected = (groups[i].strip() if groups[i] else None for i in range(3))
        priority_raw = groups[3].strip() if len(groups) > 3 and groups[3] else None
        priority = priority_raw.lower() if priority_raw else "normal"
        if priority not in ("normal", "urgent", "low"):
            priority = "normal"

        if not task:
            logger.warning(
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

    def parse_response(self, response):
        """Extrai marcadores de controle e retorna estado estruturado."""
        if response is None:
            return None, None, None, False, False, None

        app = self.app
        route_target, handoff, ack_id = None, None, None

        # Tenta envelope JSON primeiro
        envelope = self.parse_envelope(response)
        if envelope is not None:
            if envelope.type == "handoff" and envelope.route:
                if not envelope.content or not envelope.content.strip():
                    logger.warning(
                        "[HANDOFF] Rejected handoff envelope with empty content (route=%s)",
                        envelope.route,
                    )
                else:
                    route_target = envelope.route
                    handoff = {"task": envelope.content, "chain": []}
                    if envelope.metadata:
                        handoff.update(envelope.metadata)
            if envelope.type == "state_update" and envelope.state_updates:
                self.apply_state_update(json.dumps(envelope.state_updates))
            if envelope.type == "ack" and envelope.handoff_id:
                ack_id = envelope.handoff_id
            response = envelope.content
        else:
            # Fallback: fluxo regex completo
            if STATE_UPDATE_START in response:
                for state_match in self.STATE_UPDATE_PATTERN.finditer(response):
                    self.apply_state_update(state_match.group(1))
                response = self.STATE_UPDATE_PATTERN.sub("", response).strip()

            ack_match = self.ACK_PATTERN.search(response)
            if ack_match:
                ack_id = ack_match.group(1)
                response = self.ACK_PATTERN.sub("", response, count=1).strip()
                logger.info("[ACK] received ack_id=%s", ack_id)

            if ROUTE_PREFIX in response:
                match = self.ROUTE_PATTERN.search(response)
                if match:
                    raw_payload = match.group(2).strip()
                    route_target = match.group(1)
                    parsed_handoff = self.parse_handoff_payload(raw_payload, target=route_target)
                    logger.info("[ROUTE] match=%s, target=%s", match.group(0)[:100], route_target)
                    if parsed_handoff:
                        handoff = parsed_handoff
                        if hasattr(app, "session_state") and app.session_state:
                            try:
                                app.session_state["handoffs_received"] += 1
                            except KeyError:
                                pass
                    else:
                        logger.warning(
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

        # Handoff sem texto visível (ex: apenas [ROUTE:codex] task: ...) ainda
        # é uma intenção válida do agente — retorna com response=None mas
        # preservando route_target/handoff para não cair em CHAT_FAILOVER.
        if response is None:
            if route_target is not None:
                return None, route_target, handoff, False, False, ack_id
            return None, None, None, False, False, None

        extend = response.rstrip().endswith(EXTEND_MARKER)
        if extend:
            response = response.rstrip()[: -len(EXTEND_MARKER)].rstrip()

        needs_human_input = NEEDS_INPUT_MARKER in response
        if needs_human_input:
            response = response.replace(NEEDS_INPUT_MARKER, "").strip()

        return response, route_target, handoff, extend, needs_human_input, ack_id
