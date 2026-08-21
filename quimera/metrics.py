"""Sistema de métricas de entrega dos agentes.

Coleta e persiste dados brutos de entrega, sem qualquer camada de
interpretação ou feedback:
- Taxa de delegação inválida (payload malformado)
- Taxa de delegação circular detectada
- Número de turnos sem progresso (respostas vazias/irrelevantes)
- Frequência de próximos passos claros
- Tempo médio de resposta por agente
- Taxa de síntese que requer correção
- Uso e falhas de ferramentas

Os dados são consultados pelo humano via `/stats`
(ver `quimera/metrics_report.py`) e nunca injetados no prompt.
"""
import json
import atexit
import threading
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

# Debounce para save: evita I/O síncrono + serialização JSON a cada métrica.
_SAVE_DEBOUNCE_SECONDS = 5.0


@dataclass
class AgentBehaviorMetrics:
    """Métricas de comportamento de um agente específico."""
    agent_name: str

    # Contadores básicos
    responses_total: int = 0
    responses_empty: int = 0  # Respostas vazias ou sem conteúdo útil
    delegations_sent: int = 0
    delegations_received: int = 0
    delegations_invalid: int = 0  # Payload malformado
    delegations_circular_detected: int = 0

    # Qualidade de resposta
    next_steps_claros: int = 0  # Respostas com próximo passo explícito
    redundancias_detectadas: int = 0  # Respostas redundantes
    respostas_longas: int = 0  # Respostas prolixas acima do limite heurístico
    synthesis_requests: int = 0  # Quantas vezes foi chamado para sintetizar
    synthesis_corrections: int = 0  # Quantas vezes a síntese precisou de correção
    responses_code_context: int = 0  # Respostas com sinais de contexto de código/arquivo
    tool_calls_total: int = 0
    tool_calls_failed: int = 0
    invalid_tool_calls: int = 0
    tool_loop_abortions: int = 0
    tool_errors_by_type: dict[str, int] = field(default_factory=dict)
    tool_loop_abort_reasons: dict[str, int] = field(default_factory=dict)

    # Timing
    total_latency_seconds: float = 0.0
    response_count: int = 0
    total_response_chars: int = 0

    @property
    def avg_latency_seconds(self) -> float:
        """Tempo médio de resposta em segundos."""
        if self.response_count == 0:
            return 0.0
        return self.total_latency_seconds / self.response_count

    @property
    def invalid_delegation_rate(self) -> float:
        """Taxa de delegações inválidas (0.0 a 1.0)."""
        if self.delegations_sent == 0:
            return 0.0
        return self.delegations_invalid / self.delegations_sent

    @property
    def empty_response_rate(self) -> float:
        """Taxa de respostas vazias (0.0 a 1.0)."""
        if self.responses_total == 0:
            return 0.0
        return self.responses_empty / self.responses_total

    @property
    def next_step_clarity_rate(self) -> float:
        """Taxa de respostas com próximo passo claro (0.0 a 1.0)."""
        if self.responses_total == 0:
            return 0.0
        return self.next_steps_claros / self.responses_total

    @property
    def tool_success_rate(self) -> float:
        """Taxa de sucesso das chamadas de ferramenta (0.0 a 1.0)."""
        if self.tool_calls_total == 0:
            return 0.0
        return (self.tool_calls_total - self.tool_calls_failed) / self.tool_calls_total

    @property
    def avg_response_chars(self) -> float:
        """Número médio de caracteres por resposta."""
        if self.responses_total == 0:
            return 0.0
        return self.total_response_chars / self.responses_total

    @property
    def long_response_rate(self) -> float:
        """Taxa de respostas longas (0.0 a 1.0)."""
        if self.responses_total == 0:
            return 0.0
        return self.respostas_longas / self.responses_total

    def record_response(self, latency_seconds: float, has_next_step: bool = False,
                        is_empty: bool = False, is_redundant: bool = False,
                        response_text: str | None = None):
        """Registra uma resposta do agente."""
        self.responses_total += 1
        self.response_count += 1
        self.total_latency_seconds += latency_seconds
        response_length = len((response_text or "").strip())
        self.total_response_chars += response_length
        if has_next_step:
            self.next_steps_claros += 1
        if is_empty:
            self.responses_empty += 1
        if is_redundant:
            self.redundancias_detectadas += 1
        if response_length >= 280:
            self.respostas_longas += 1
        if self._looks_like_code_context(response_text):
            self.responses_code_context += 1

    @staticmethod
    def _looks_like_code_context(response_text: str | None) -> bool:
        """Detecta heurísticamente respostas sobre arquivos/código."""
        if not response_text:
            return False
        text = response_text.lower()
        indicators = (
            ".py",
            ".md",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".sh",
            "arquivo",
            "arquivos",
            "código",
            "code",
            "path",
            "paths",
            "linha",
            "linhas",
            "função",
            "function",
            "classe",
            "class",
            "teste",
            "testes",
        )
        return any(indicator in text for indicator in indicators)

    def record_delegation_sent(self, is_invalid: bool = False):
        """Registra uma delegação enviada pelo agente."""
        self.delegations_sent += 1
        if is_invalid:
            self.delegations_invalid += 1

    def record_delegation_received(self, is_circular: bool = False):
        """Registra uma delegação recebida pelo agente."""
        self.delegations_received += 1
        if is_circular:
            self.delegations_circular_detected += 1

    def record_synthesis(self, needed_correction: bool = False):
        """Registra uma operação de síntese."""
        self.synthesis_requests += 1
        if needed_correction:
            self.synthesis_corrections += 1

    def record_tool_call(self, ok: bool, is_invalid: bool = False, error_type: str | None = None):
        """Registra o resultado de uma chamada de ferramenta."""
        self.tool_calls_total += 1
        if not ok:
            self.tool_calls_failed += 1
            normalized_type = str(error_type or "generic")
            self.tool_errors_by_type[normalized_type] = self.tool_errors_by_type.get(normalized_type, 0) + 1
        if is_invalid:
            self.invalid_tool_calls += 1

    def record_tool_loop_abort(self, reason: str | None = None):
        """Registra um aborto de loop de ferramenta."""
        self.tool_loop_abortions += 1
        reason_key = str(reason or "unknown")
        self.tool_loop_abort_reasons[reason_key] = self.tool_loop_abort_reasons.get(reason_key, 0) + 1

    def to_dict(self) -> dict:
        """Converte para dicionário para serialização JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'AgentBehaviorMetrics':
        """Cria instância a partir de um dicionário."""
        merged = dict(data)
        merged.setdefault("tool_errors_by_type", {})
        merged.setdefault("tool_loop_abort_reasons", {})
        return cls(**merged)


class BehaviorMetricsTracker:
    """Rastreia métricas de comportamento de todos os agentes."""

    def __init__(self, storage_path: Path | str | None = None):
        """Inicializa o rastreador com carregamento opcional de dados persistidos."""
        self._metrics: dict[str, AgentBehaviorMetrics] = {}
        self._storage_path = Path(storage_path) if storage_path else None
        self._last_save_time: float = 0.0
        self._save_dirty = False
        self._save_timer: threading.Timer | None = None
        if self._storage_path:
            self.load()
        atexit.register(self._flush_if_dirty)

    def load(self):
        """Carrega métricas do armazenamento persistente."""
        if not self._storage_path or not self._storage_path.exists():
            return 0

        try:
            data = json.loads(self._storage_path.read_text(encoding="utf-8"))
            for agent_name, metrics_data in data.items():
                self._metrics[agent_name] = AgentBehaviorMetrics.from_dict(metrics_data)
            return len(data)
        except (json.JSONDecodeError, OSError, TypeError, KeyError):
            # Se falhar ao carregar, ignora e começa do zero
            return 0

    def save(self, force: bool = False):
        """Grava métricas no armazenamento persistente com debounce."""
        if not self._storage_path:
            return
        now = time.monotonic()
        if not force and self._last_save_time > 0.0 and (now - self._last_save_time) < _SAVE_DEBOUNCE_SECONDS:
            self._schedule_flush()
            return
        self._save_dirty = False
        self._last_save_time = now

        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            data = {name: metrics.to_dict() for name, metrics in self._metrics.items()}
            self._storage_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8"
            )
        except Exception as e:
            print(f"[metrics] Falha ao salvar métricas: {e}", file=sys.stderr)

    def _mark_dirty(self):
        """Marca dados como pendentes de persistência e agenda flush."""
        if not self._storage_path:
            return
        if not self._save_dirty:
            self._save_dirty = True
            self._last_save_time = 0.0  # força próximo save() a gravar imediatamente
        self.save()
        self._schedule_flush()

    def _schedule_flush(self):
        """Agenda um flush para daqui a _SAVE_DEBOUNCE_SECONDS, cancelando timer anterior."""
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(_SAVE_DEBOUNCE_SECONDS, self._flush_if_dirty)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _flush_if_dirty(self):
        """Salva se houver alterações pendentes (chamado em shutdown)."""
        if self._save_timer:
            self._save_timer.cancel()
            self._save_timer = None
        if self._save_dirty:
            self.save(force=True)

    def get_agent(self, agent_name: str) -> AgentBehaviorMetrics:
        """Obtém ou cria métricas para um agente."""
        if agent_name not in self._metrics:
            self._metrics[agent_name] = AgentBehaviorMetrics(agent_name=agent_name)
        return self._metrics[agent_name]

    def record_response(self, agent_name: str, latency_seconds: float,
                        has_next_step: bool = False, is_empty: bool = False,
                        is_redundant: bool = False, response_text: str | None = None):
        """Registra uma resposta."""
        metrics = self.get_agent(agent_name)
        metrics.record_response(
            latency_seconds,
            has_next_step,
            is_empty,
            is_redundant,
            response_text=response_text,
        )
        self._mark_dirty()

    def record_delegation_sent(self, agent_name: str, is_invalid: bool = False):
        """Registra uma delegação enviada."""
        metrics = self.get_agent(agent_name)
        metrics.record_delegation_sent(is_invalid)
        self._mark_dirty()

    def record_delegation_received(self, agent_name: str, is_circular: bool = False):
        """Registra uma delegação recebida."""
        metrics = self.get_agent(agent_name)
        metrics.record_delegation_received(is_circular)
        self._mark_dirty()

    def record_synthesis(self, agent_name: str, needed_correction: bool = False):
        """Registra uma operação de síntese."""
        metrics = self.get_agent(agent_name)
        metrics.record_synthesis(needed_correction)
        self._mark_dirty()

    def record_tool_call(
        self,
        agent_name: str,
        ok: bool,
        is_invalid: bool = False,
        error_type: str | None = None,
    ):
        """Registra uma chamada de ferramenta associada a um agente."""
        metrics = self.get_agent(agent_name)
        metrics.record_tool_call(ok=ok, is_invalid=is_invalid, error_type=error_type)
        self._mark_dirty()

    def record_tool_loop_abort(self, agent_name: str, reason: str | None = None):
        """Registra um aborto de loop de ferramentas para um agente."""
        metrics = self.get_agent(agent_name)
        metrics.record_tool_loop_abort(reason=reason)
        self._mark_dirty()

    def get_agent_summary(self, agent_name: str) -> dict:
        """Retorna resumo das métricas de um agente."""
        metrics = self.get_agent(agent_name)
        return {
            "agent": agent_name,
            "responses_total": metrics.responses_total,
            "avg_latency_seconds": round(metrics.avg_latency_seconds, 2),
            "invalid_delegation_rate": round(metrics.invalid_delegation_rate, 3),
            "empty_response_rate": round(metrics.empty_response_rate, 3),
            "next_step_clarity_rate": round(metrics.next_step_clarity_rate, 3),
            "delegations_sent": metrics.delegations_sent,
            "delegations_received": metrics.delegations_received,
            "circular_detections": metrics.delegations_circular_detected,
            "redundancias": metrics.redundancias_detectadas,
            "respostas_longas": metrics.respostas_longas,
            "avg_response_chars": round(metrics.avg_response_chars, 1),
            "long_response_rate": round(metrics.long_response_rate, 3),
            "synthesis_requests": metrics.synthesis_requests,
            "synthesis_corrections": metrics.synthesis_corrections,
            "tool_calls_total": metrics.tool_calls_total,
            "tool_calls_failed": metrics.tool_calls_failed,
            "invalid_tool_calls": metrics.invalid_tool_calls,
            "tool_loop_abortions": metrics.tool_loop_abortions,
            "tool_errors_by_type": dict(metrics.tool_errors_by_type),
            "tool_loop_abort_reasons": dict(metrics.tool_loop_abort_reasons),
            "tool_success_rate": round(metrics.tool_success_rate, 3),
        }

    def get_all_summaries(self) -> list[dict]:
        """Retorna resumo de todos os agentes."""
        return [
            self.get_agent_summary(name)
            for name in sorted(self._metrics.keys())
        ]

    def known_agents(self) -> list[str]:
        """Retorna os agentes com métricas acumuladas, em ordem alfabética."""
        return sorted(self._metrics.keys())

    def has_metrics(self, agent_name: str) -> bool:
        """Indica se já existem métricas registradas para o agente."""
        return agent_name in self._metrics

    def reset_agent(self, agent_name: str) -> bool:
        """Descarta as métricas de um agente, persistindo a remoção."""
        if agent_name not in self._metrics:
            return False
        del self._metrics[agent_name]
        self._mark_dirty()
        return True

    def reset_all(self) -> int:
        """Descarta as métricas de todos os agentes e devolve quantos foram removidos."""
        removed = len(self._metrics)
        if not removed:
            return 0
        self._metrics.clear()
        self._mark_dirty()
        return removed
