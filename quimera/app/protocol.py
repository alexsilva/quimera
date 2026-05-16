"""Componentes de `quimera.app.protocol`."""
import hashlib
import json
import re
import time
from dataclasses import dataclass

from ..constants import EXTEND_MARKER, NEEDS_INPUT_MARKER, STATE_UPDATE_START
from ..shared_state import is_agent_state_key, normalize_state_key, validate_agent_state_value
from . import logger


@dataclass
class ProtocolEnvelope:
    type: str
    content: str
    route: str | None = None
    handoffs: list[dict] | None = None
    legacy_routes_present: bool = False
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
    ACK_PATTERN = re.compile(r"^\s*\[ACK:([A-Za-z0-9]+)\]\s*", re.M)
    PAYLOAD_FIELD_RE = re.compile(r"^\s*(task|context|expected)\s*:", re.IGNORECASE)
    PROTOCOL_ENVELOPE_TYPES = ('"type": "handoff"', '"type": "state_update"', '"type": "ack"')

    def __init__(self, lock, shared_state, workspace=None, decisions_log_path=None) -> None:
        """Inicializa uma instância de AppProtocol."""
        self._lock = lock
        self._shared_state = shared_state
        self._workspace = workspace
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

        with self._lock:
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
                else:
                    self._shared_state[normalized_key] = merged
                    if normalized_key == "decisions" and isinstance(value, list):
                        dlogger = self._get_decisions_logger()
                        if dlogger:
                            for item in value:
                                dlogger.append(item, {
                                    "workspace": str(self._workspace.cwd) if self._workspace else None})
        return True

    @staticmethod
    def generate_handoff_id(task, target, timestamp=None):
        """Generate a deterministic ID for a handoff based on task content and target."""
        ts = timestamp or time.time()
        raw = f"{ts}:{target}:{task}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    @staticmethod
    def _find_envelope_in_text(text):
        """Procura envelope JSON embutido em texto com conteúdo ao redor.
        Retorna (ProtocolEnvelope, before_text, after_text) ou None."""
        brace_depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == '{':
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
                if brace_depth == 0 and start >= 0:
                    candidate = text[start:i+1]
                    envelope = AppProtocol.parse_envelope(candidate)
                    if envelope is not None:
                        before = text[:start]
                        after = text[i+1:]
                        return envelope, before, after
        return None

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
        handoffs_raw = data.get("handoffs")
        handoffs = None
        if isinstance(handoffs_raw, list):
            handoffs = []
            for item in handoffs_raw:
                if not isinstance(item, dict):
                    continue
                route = item.get("route")
                content = item.get("content")
                if not isinstance(route, str) or not isinstance(content, str):
                    continue
                route = route.strip()
                content = content.strip()
                if not route or not content:
                    continue
                normalized = {
                    "route": route,
                    "content": content,
                }
                if isinstance(item.get("metadata"), dict):
                    normalized["metadata"] = item["metadata"]
                if isinstance(item.get("handoff_id"), str) and item["handoff_id"].strip():
                    normalized["handoff_id"] = item["handoff_id"].strip()
                handoffs.append(normalized)

        return ProtocolEnvelope(
            type=str(data["type"]),
            content=str(data.get("content", "")),
            route=str(data["route"]) if data.get("route") else None,
            handoffs=handoffs,
            legacy_routes_present="routes" in data,
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
        if envelope.handoffs:
            for item in envelope.handoffs:
                if not item.get("route"):
                    return False, "handoff item missing 'route' target"
                if not item.get("content") or not item["content"].strip():
                    return False, "handoff item missing 'content' (task description)"
            return True, ""
        if not envelope.route:
            return False, "handoff missing 'route' target"
        if not envelope.content or not envelope.content.strip():
            return False, "handoff missing 'content' (task description)"
        return True, ""

    def _build_handoff_message(self, route, content, metadata=None, handoff_id=None):
        handoff = {"task": content, "chain": []}
        if isinstance(metadata, dict):
            handoff.update(metadata)
        if "priority" not in handoff:
            handoff["priority"] = "normal"
        handoff["handoff_id"] = handoff_id or self.generate_handoff_id(content, route)
        return handoff

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

        route_target, handoff, ack_id = None, None, None

        embedded = self._find_envelope_in_text(response)
        if embedded is not None:
            envelope, before_text, after_text = embedded
            if envelope.type == "handoff":
                if envelope.handoffs:
                    first_item = envelope.handoffs[0] if envelope.handoffs else None
                    if not first_item:
                        logger.warning("[HANDOFF] Rejected handoff envelope — empty handoffs list")
                    else:
                        route_target = first_item["route"]
                        handoff = self._build_handoff_message(
                            route_target,
                            first_item["content"],
                            first_item.get("metadata"),
                            first_item.get("handoff_id"),
                        )
                        if len(envelope.handoffs) > 1:
                            handoff["_pending_handoffs"] = envelope.handoffs[1:]
                elif envelope.legacy_routes_present:
                    logger.warning("[HANDOFF] Rejected legacy handoff envelope using 'routes'")
                elif not envelope.route or not envelope.content or not envelope.content.strip():
                    logger.warning(
                        "[HANDOFF] Rejected handoff envelope — missing route or empty content (route=%s)",
                        envelope.route,
                    )
                else:
                    route_target = envelope.route
                    handoff = self._build_handoff_message(
                        route_target,
                        envelope.content,
                        envelope.metadata,
                        envelope.handoff_id,
                    )
            if envelope.type == "state_update" and envelope.state_updates:
                self.apply_state_update(json.dumps(envelope.state_updates))
            if envelope.type == "ack" and envelope.handoff_id:
                ack_id = envelope.handoff_id
            parts = [p.strip() for p in [before_text, after_text] if p.strip()]
            if parts:
                response = "\n".join(parts)
            elif envelope.type == "handoff":
                response = None
            else:
                response = envelope.content
        else:
            if STATE_UPDATE_START in response:
                for state_match in self.STATE_UPDATE_PATTERN.finditer(response):
                    self.apply_state_update(state_match.group(1))
                response = self.STATE_UPDATE_PATTERN.sub("", response).strip()

            ack_match = self.ACK_PATTERN.search(response)
            if ack_match:
                ack_id = ack_match.group(1)
                response = self.ACK_PATTERN.sub("", response, count=1).strip()
                logger.info("[ACK] received ack_id=%s", ack_id)

        # Handoff sem texto visível (apenas envelope JSON que foi consumido)
        # ainda é uma intenção válida do agente — retorna com response=None mas
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
