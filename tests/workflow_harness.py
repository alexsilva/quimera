"""Harness de integração dos workflows: executores, runner e reviewer reais, agentes scriptados.

Só o ponto de despacho ao agente (``delegate``) é simulado; claim, estado,
dependências, fences, revisão e coordenação usam o código de produção.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from quimera.tasks.classifiers import classify_task_execution_result, classify_task_review_result
from quimera.tasks.executor import TaskExecutor
from quimera.tasks.failover import TaskFailoverPolicy
from quimera.tasks.repository import TaskRepository
from quimera.tasks.reviewer import TaskReviewer
from quimera.tasks.runner import TaskRunner
from quimera.workflows.coordinator import WorkflowCoordinator
from quimera.workflows.execution import WorkflowTaskHooks
from quimera.workflows.models import WorkflowLimits
from quimera.workflows.repository import WorkflowRepository


def handoff_text(summary: str, *, files=(), checks=(), refs=(), status="success", limitations=()) -> str:
    payload = {
        "handoff": {
            "status": status,
            "summary": summary,
            "files_changed": list(files),
            "checks": [{"command": c, "outcome": "passed"} for c in checks],
            "evidence_refs": list(refs),
            "limitations": list(limitations),
            "failure_reason": None,
        }
    }
    return f"Trabalho feito.\n```json\n{json.dumps(payload)}\n```"


def plan_payload(tasks, objective="Corrigir sincronizacao", **extra):
    return {
        "objective": objective,
        "global_criteria": ["fluxos validados"],
        "constraints": ["nao alterar migracoes"],
        "work_items": tasks,
        **extra,
    }


def task_spec(item_id, *, deps=(), task_type="bug_investigation", writes=None, evidence=(), **extra):
    spec = {
        "id": item_id,
        "title": f"Tarefa {item_id}",
        "description": f"Descricao da tarefa {item_id}",
        "task_type": task_type,
        "dependencies": list(deps),
        "acceptance_criteria": [f"{item_id} entregue com evidencia"],
        "evidence_expected": list(evidence),
        **extra,
    }
    if writes is not None:
        spec["writes"] = writes
    return spec


@dataclass
class _Profile:
    name: str
    prefix: str = ""
    aliases: tuple = ()
    supports_task_execution: bool = True


class _Silent:
    def show_muted_message(self, message):
        pass


@dataclass
class Harness:
    tmp_path: Path
    agents: list[str]
    scripts: dict = field(default_factory=dict)
    limits: WorkflowLimits = field(default_factory=WorkflowLimits)
    interrupted: list = field(default_factory=list)
    notices: list = field(default_factory=list)
    consults: list = field(default_factory=list)
    delegations: list = field(default_factory=list)
    consult_answer: str | None = None
    active: list = field(default_factory=list)

    def __post_init__(self):
        self.db = str(self.tmp_path / "tasks.db")
        self.tasks = TaskRepository(self.db)
        self.job_id = self._new_job()
        self.workflows = WorkflowRepository(self.tasks, self.limits)
        self.active = list(self.agents)
        self.live: set[int] = set()
        self._lock = threading.Lock()
        self.coordinator = self._coordinator()
        self.executors: list[TaskExecutor] = []

    def _new_job(self) -> int:
        conn = self.tasks._conn()
        try:
            cur = conn.execute(
                "INSERT INTO jobs(description, status, created_at, updated_at) "
                "VALUES ('job', 'active', datetime('now'), datetime('now'))"
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _coordinator(self) -> WorkflowCoordinator:
        return WorkflowCoordinator(
            self.workflows,
            self.tasks,
            get_active_agents=lambda: list(self.active),
            get_job_id=lambda: self.job_id,
            interrupt_tasks=lambda ids: self.interrupted.append(tuple(ids)),
            live_task_ids=lambda: set(self.live),
            notify=self.notices.append,
            consult=self._consult,
        )

    def new_session(self) -> WorkflowCoordinator:
        """Simula reinício do app: novo job e novo coordenador sobre o mesmo banco."""
        self.stop()
        self.job_id = self._new_job()
        self.coordinator = self._coordinator()
        return self.coordinator

    def _consult(self, workflow, attention, status_text):
        self.consults.append((workflow.id, attention, status_text))
        return self.consult_answer

    # ── execução ───────────────────────────────────────────────────────

    def delegate(self, agent, *, delegation, **kwargs):
        with self._lock:
            self.delegations.append((agent, delegation))
        script = self.scripts.get(agent) or self.scripts.get("*")
        return script(agent, delegation)

    def start(self, poll_interval: float = 0.05) -> None:
        policy = TaskFailoverPolicy(
            active_agents=lambda: list(self.active),
            get_agent_profile=lambda name: _Profile(name=name),
            repository=self.tasks,
        )
        hooks = WorkflowTaskHooks(self.workflows, self.tasks, self.tmp_path)
        harness = self

        class _Dispatch:
            def delegate(self, agent, **kw):
                task_id = int(str(kw["delegation"]["delegation_id"]).rsplit("-", 1)[1])
                harness.live.add(task_id)
                try:
                    return harness.delegate(agent, **kw)
                finally:
                    harness.live.discard(task_id)

        runner = TaskRunner(
            _Dispatch(), _Silent(), self.tasks, policy,
            classify_task_execution_result, lambda: False, task_hooks=hooks,
        )
        reviewer = TaskReviewer(
            _Dispatch(), _Silent(), self.tasks, policy,
            classify_task_review_result, lambda: False, task_hooks=hooks,
        )
        for agent in self.agents:
            executor = TaskExecutor(
                agent, job_id=self.job_id, repository=self.tasks,
                max_workers=2, poll_interval=poll_interval,
            )
            executor.set_handler(runner.handler_for(agent))
            executor.set_review_handler(reviewer.handler_for(agent))
            executor.set_review_eligibility(lambda a=agent: a in self.active)
            executor.start()
            self.executors.append(executor)

    def stop(self) -> None:
        for executor in self.executors:
            executor.stop()
        self.executors = []

    def wait_for(self, predicate, timeout: float = 20.0, step: float = 0.05) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.coordinator.tick()
            if predicate():
                return True
            time.sleep(step)
        return predicate()

    def statuses(self, workflow_id: int) -> dict[str, str]:
        snap = self.workflows.snapshot(workflow_id)
        return {view.link.item_id: view.status for view in snap.tasks}
