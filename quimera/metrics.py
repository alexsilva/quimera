"""Sistema de métricas de comportamento dos agentes.

Rastreia métricas de eficiência colaborativa:
- Taxa de handoff inválido (payload malformado)
- Taxa de handoff circular detectado
- Número de turnos sem progresso (respostas vazias/irrelevantes)
- Frequência de próximos passos claros
- Tempo médio de resposta por agente
- Taxa de síntese que requer correção
"""
from collections import defaultdict
from dataclasses import dataclass, field, asdict
import time
import json
from pathlib import Path


@dataclass
class AgentBehaviorMetrics:
    """Métricas de comportamento de um agente específico."""
    agent_name: str
    
    # Contadores básicos
    responses_total: int = 0
    responses_empty: int = 0  # Respostas vazias ou sem conteúdo útil
    handoffs_sent: int = 0
    handoffs_received: int = 0
    handoffs_invalid: int = 0  # Payload malformado
    handoffs_circular_detected: int = 0
    
    # Qualidade de resposta
    next_steps_claros: int = 0  # Respostas com próximo passo explícito
    redundancias_detectadas: int = 0  # Respostas redundantes
    synthesis_requests: int = 0  # Quantas vezes foi chamado para sintetizar
    synthesis_corrections: int = 0  # Quantas vezes a síntese precisou de correção
    
    # Timing
    total_latency_seconds: float = 0.0
    response_count: int = 0
    
    @property
    def avg_latency_seconds(self) -> float:
        """Tempo médio de resposta em segundos."""
        if self.response_count == 0:
            return 0.0
        return self.total_latency_seconds / self.response_count
    
    @property
    def invalid_handoff_rate(self) -> float:
        """Taxa de handoffs inválidos (0.0 a 1.0)."""
        if self.handoffs_sent == 0:
            return 0.0
        return self.handoffs_invalid / self.handoffs_sent
    
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
    
    def record_response(self, latency_seconds: float, has_next_step: bool = False, 
                        is_empty: bool = False, is_redundant: bool = False):
        """Registra uma resposta do agente."""
        self.responses_total += 1
        self.response_count += 1
        self.total_latency_seconds += latency_seconds
        if has_next_step:
            self.next_steps_claros += 1
        if is_empty:
            self.responses_empty += 1
        if is_redundant:
            self.redundancias_detectadas += 1
    
    def record_handoff_sent(self, is_invalid: bool = False):
        """Registra um handoff enviado pelo agente."""
        self.handoffs_sent += 1
        if is_invalid:
            self.handoffs_invalid += 1
    
    def record_handoff_received(self, is_circular: bool = False):
        """Registra um handoff recebido pelo agente."""
        self.handoffs_received += 1
        if is_circular:
            self.handoffs_circular_detected += 1
    
    def record_synthesis(self, needed_correction: bool = False):
        """Registra uma operação de síntese."""
        self.synthesis_requests += 1
        if needed_correction:
            self.synthesis_corrections += 1

    def to_dict(self) -> dict:
        """Converte para dicionário para serialização JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'AgentBehaviorMetrics':
        """Cria instância a partir de um dicionário."""
        return cls(**data)


class BehaviorMetricsTracker:
    """Rastreia métricas de comportamento de todos os agentes."""
    
    def __init__(self, storage_path: Path | str | None = None):
        """Inicializa uma instância de BehaviorMetricsTracker."""
        self._metrics: dict[str, AgentBehaviorMetrics] = {}
        self._storage_path = Path(storage_path) if storage_path else None
        if self._storage_path:
            self.load()
    
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

    def save(self):
        """Grava métricas no armazenamento persistente."""
        if not self._storage_path:
            return
            
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            data = {name: metrics.to_dict() for name, metrics in self._metrics.items()}
            self._storage_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8"
            )
        except Exception as e:
            import sys
            print(f"[metrics] Falha ao salvar métricas: {e}", file=sys.stderr)

    def get_agent(self, agent_name: str) -> AgentBehaviorMetrics:
        """Obtém ou cria métricas para um agente."""
        if agent_name not in self._metrics:
            self._metrics[agent_name] = AgentBehaviorMetrics(agent_name=agent_name)
        return self._metrics[agent_name]
    
    def record_response(self, agent_name: str, latency_seconds: float,
                        has_next_step: bool = False, is_empty: bool = False,
                        is_redundant: bool = False):
        """Registra uma resposta."""
        metrics = self.get_agent(agent_name)
        metrics.record_response(latency_seconds, has_next_step, is_empty, is_redundant)
        self.save()
    
    def record_handoff_sent(self, agent_name: str, is_invalid: bool = False):
        """Registra um handoff enviado."""
        metrics = self.get_agent(agent_name)
        metrics.record_handoff_sent(is_invalid)
        self.save()
    
    def record_handoff_received(self, agent_name: str, is_circular: bool = False):
        """Registra um handoff recebido."""
        metrics = self.get_agent(agent_name)
        metrics.record_handoff_received(is_circular)
        self.save()
    
    def record_synthesis(self, agent_name: str, needed_correction: bool = False):
        """Registra uma operação de síntese."""
        metrics = self.get_agent(agent_name)
        metrics.record_synthesis(needed_correction)
        self.save()
    
    def get_agent_summary(self, agent_name: str) -> dict:
        """Retorna resumo das métricas de um agente."""
        metrics = self.get_agent(agent_name)
        return {
            "agent": agent_name,
            "responses_total": metrics.responses_total,
            "avg_latency_seconds": round(metrics.avg_latency_seconds, 2),
            "invalid_handoff_rate": round(metrics.invalid_handoff_rate, 3),
            "empty_response_rate": round(metrics.empty_response_rate, 3),
            "next_step_clarity_rate": round(metrics.next_step_clarity_rate, 3),
            "handoffs_sent": metrics.handoffs_sent,
            "handoffs_received": metrics.handoffs_received,
            "circular_detections": metrics.handoffs_circular_detected,
            "redundancias": metrics.redundancias_detectadas,
            "synthesis_requests": metrics.synthesis_requests,
            "synthesis_corrections": metrics.synthesis_corrections,
        }
    
    def get_all_summaries(self) -> list[dict]:
        """Retorna resumo de todos os agentes."""
        return [
            self.get_agent_summary(name)
            for name in sorted(self._metrics.keys())
        ]

    def get_position_summary(self, agent_name: str) -> str:
        """Retorna resumo da posição do agente baseado no histórico persistido.

        Diferente de generate_feedback (que só ativa com thresholds), este método
        SEMPRE mostra o estado atual quando há métricas acumuladas.
        """
        metrics = self.get_agent(agent_name)
        if metrics.responses_total == 0:
            return ""

        parts = [
            f"- SEU HISTÓRICO ({metrics.responses_total} respostas em sessões anteriores):",
            f"  Latência média: {metrics.avg_latency_seconds:.1f}s | "
            f"Handoffs: {metrics.handoffs_sent} enviados, {metrics.handoffs_received} recebidos | "
            f"Próximos passos claros: {metrics.next_step_clarity_rate:.0%}",
        ]

        warnings = []
        if metrics.invalid_handoff_rate > 0.3:
            warnings.append(f"handoffs inválidos {metrics.invalid_handoff_rate:.0%}")
        if metrics.empty_response_rate > 0.2:
            warnings.append(f"respostas vazias {metrics.empty_response_rate:.0%}")
        if metrics.handoffs_circular_detected > 0:
            warnings.append(f"{metrics.handoffs_circular_detected} delegações circulares")
        if metrics.synthesis_requests >= 3 and metrics.synthesis_corrections / metrics.synthesis_requests > 0.5:
            warnings.append(f"sínteses com correção {metrics.synthesis_corrections}/{metrics.synthesis_requests}")
        if metrics.avg_latency_seconds > 30 and metrics.response_count >= 5:
            warnings.append(f"latência alta ({metrics.avg_latency_seconds:.1f}s)")

        if warnings:
            parts.append(f"  Atenção: {', '.join(warnings)}")

        return "\n".join(parts)

    def generate_feedback(self, agent_name: str) -> str:
        """Gera feedback acionável baseado nas métricas do agente."""
        metrics = self.get_agent(agent_name)
        
        if metrics.responses_total < 3:
            return ""  # Não há dados suficientes
        
        feedback_parts = []
        
        # Taxa de handoff inválido alta
        if metrics.invalid_handoff_rate > 0.3:
            feedback_parts.append(
                f"- ALTA TAXA DE HANDOFF INVÁLIDO ({metrics.invalid_handoff_rate:.0%}):\n"
                "  Verifique o formato [ROUTE:agente] task: ...\n"
                "  Use apenas '|' OU quebra de linha para separar campos, nunca misture.\n"
                "  Se não tiver contexto suficiente, resolva você mesmo."
            )
        
        # Respostas vazias frequentes
        if metrics.empty_response_rate > 0.2:
            feedback_parts.append(
                f"- RESPOSTAS VAZIAS ({metrics.empty_response_rate:.0%}):\n"
                "  Garanta que sua resposta contém informação concreta e próxima ação.\n"
                "  Cada resposta deve avançar a tarefa."
            )
        
        # Falta de próximos passos claros
        if metrics.next_step_clarity_rate < 0.3 and metrics.responses_total >= 5:
            feedback_parts.append(
                f"- FALTA PRÓXIMO PASSO ({metrics.next_step_clarity_rate:.0%} das respostas):\n"
                "  Finalize sempre indicando o que fazer a seguir: continuar, pedir input, ou finalizar."
            )
        
        # Redundância detectada
        if metrics.redundancias_detectadas > 2:
            feedback_parts.append(
                f"- RESPOSTAS REDUNDANTES ({metrics.redundancias_detectadas}x):\n"
                "  Construa sobre o trabalho anterior, não repita o que já foi dito.\n"
                "  Se outro agente já resolveu, complemente ou avance."
            )
        
        # Síntese requer correção frequentemente
        if metrics.synthesis_requests >= 3 and (
            metrics.synthesis_corrections / metrics.synthesis_requests > 0.5
        ):
            feedback_parts.append(
                f"- SÍNTESES IMPRECISAS ({metrics.synthesis_corrections}/{metrics.synthesis_requests}):\n"
                "  Incorpore a resposta do agente delegado, não a repita.\n"
                "  Avance o diálogo em vez de resumir."
            )
        
        # Tempo de resposta muito alto
        if metrics.avg_latency_seconds > 30 and metrics.response_count >= 5:
            feedback_parts.append(
                f"- LATÊNCIA ALTA ({metrics.avg_latency_seconds:.1f}s média):\n"
                "  Considere respostas mais diretas e menos análise."
            )
        
        # Detecção circular alta
        if metrics.handoffs_circular_detected > 1:
            feedback_parts.append(
                f"- DELEGAÇÕES CIRCULARES ({metrics.handoffs_circular_detected}x):\n"
                "  Verifique a cadeia antes de delegar. Se já participou, resolva diretamente."
            )
        
        # Taxa de sucesso baixa
        if metrics.handoffs_sent > 3:
            success_rate = (metrics.handoffs_sent - metrics.handoffs_invalid) / metrics.handoffs_sent
            if success_rate < 0.7:
                feedback_parts.append(
                    f"- BAIXA TAXA DE SUCESSO EM HANDOFFS ({success_rate:.0%}):\n"
                    "  Revise o formato do payload ou resolva sem delegar."
                )
        
        summary = (
            f"- STATUS OPERACIONAL: {metrics.responses_total} turnos registrados.\n"
            f"  Latência média: {metrics.avg_latency_seconds:.1f}s | "
            f"Próximo passo claro: {metrics.next_step_clarity_rate:.0%}"
        )
        
        if not feedback_parts:
            return summary
        
        header = summary + "\n" + "\n".join(feedback_parts)
        return header
