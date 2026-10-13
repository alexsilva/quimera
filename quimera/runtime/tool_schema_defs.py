"""Definições do schema de ferramentas e renderização do prompt."""
from __future__ import annotations


TOOL_SCHEMA: dict[str, dict] = {
    "list_files": {
        "name": "list_files",
        "description": "Lista arquivos e diretórios em um caminho específico",
        "parameters": {
            "path": {"type": "str", "description": "Caminho do diretório", "required": True}
        },
    },
    "read_file": {
        "name": "read_file",
        "description": "Lê o conteúdo de um arquivo, opcionalmente com intervalo de linhas",
        "parameters": {
            "path": {"type": "str", "description": "Caminho absoluto do arquivo", "required": True},
            "start_line": {"type": "int", "description": "Primeira linha (1-indexed, inclusiva)", "required": False},
            "end_line": {"type": "int", "description": "Última linha (1-indexed, inclusiva)", "required": False}
        },
    },
    "write_file": {
        "name": "write_file",
        "description": "Cria arquivo novo; para reescrever um existente, requer read_file prévio no mesmo run",
        "parameters": {
            "path": {"type": "str", "description": "Caminho absoluto do arquivo", "required": True},
            "content": {"type": "str", "description": "Conteúdo a escrever", "required": True},
            "replace_existing": {"type": "bool",
                                 "description": "Use true apenas para sobrescrever arquivo existente por completo",
                                 "required": False},
        },
    },
    "apply_patch": {
        "name": "apply_patch",
        "description": "Aplica patch estruturado; Update/Delete requerem read_file prévio no mesmo run",
        "parameters": {
            "patch": {"type": "str", "description": "Patch no formato *** Begin Patch ... *** End Patch",
                      "required": True}
        },
    },
    "grep_search": {
        "name": "grep_search",
        "description": "Busca um padrão em arquivos de um diretório",
        "parameters": {
            "pattern": {"type": "str", "description": "Substring literal a buscar (não suporta regex)", "required": True},
            "path": {"type": "str", "description": "Diretório base", "required": False},
            "include_glob": {"type": "str|list[str]", "description": "Filtro glob opcional para paths retornados", "required": False},
            "exclude_dirs": {"type": "list[str]", "description": "Diretórios adicionais a ignorar", "required": False},
            "max_results": {"type": "int", "description": "Limite opcional de resultados", "required": False},
        },
    },
    "run_shell": {
        "name": "run_shell",
        "description": "Executa um comando no terminal",
        "parameters": {
            "command": {"type": "str", "description": "Comando shell", "required": True}
        },
    },
    "exec_command": {
        "name": "exec_command",
        "description": "Executa um comando com sessão persistente, polling e stdin opcional",
        "parameters": {
            "cmd": {"type": "str", "description": "Comando shell", "required": True},
            "workdir": {"type": "str", "description": "Diretório relativo ao workspace", "required": False},
            "yield_time_ms": {"type": "int", "description": "Espera por saída parcial antes de retornar",
                              "required": False},
            "tty": {"type": "bool", "description": "Executa em PTY simplificado", "required": False},
        },
    },
    "write_stdin": {
        "name": "write_stdin",
        "description": "Envia texto ao stdin de uma sessão aberta por exec_command ou faz polling",
        "parameters": {
            "session_id": {"type": "int", "description": "ID retornado por exec_command", "required": True},
            "chars": {"type": "str", "description": "Texto a enviar; vazio faz apenas polling", "required": False},
            "yield_time_ms": {"type": "int", "description": "Espera por nova saída", "required": False},
        },
    },
    "close_command_session": {
        "name": "close_command_session",
        "description": "Fecha explicitamente uma sessão aberta por exec_command",
        "parameters": {
            "session_id": {"type": "int", "description": "ID da sessão", "required": True},
        },
    },
    "tasks_create": {
        "name": "tasks_create",
        "description": "Cria uma task como o comando /task: o roteador escolhe o executor e outro agente revisa",
        "parameters": {
            "description": {"type": "str", "description": "Descrição da task", "required": True},
        },
    },
    "workflows_create": {
        "name": "workflows_create",
        "description": "Publica um plano de tarefas com dependências (objetivo, tasks, critérios); dry_run só valida",
        "parameters": {
            "objective": {"type": "str", "description": "Objetivo global do usuário", "required": True},
            "work_items": {"type": "list", "description": "Tasks do plano: id, title, description, task_type, dependencies, acceptance_criteria, evidence_expected, assigned_to, writes", "required": True},
            "global_criteria": {"type": "list", "description": "Critérios globais de conclusão", "required": False},
            "constraints": {"type": "list", "description": "Restrições e escopo permitido", "required": False},
            "idempotency_key": {"type": "str", "description": "Chave para republicar sem duplicar", "required": False},
            "dry_run": {"type": "bool", "description": "Só valida o plano, sem persistir", "required": False},
        },
    },
    "workflows_start": {
        "name": "workflows_start",
        "description": "Inicia a execução distribuída de um workflow publicado (idempotente)",
        "parameters": {
            "workflow_id": {"type": "int", "description": "ID do workflow", "required": True},
        },
    },
    "workflows_status": {
        "name": "workflows_status",
        "description": "Estado, tasks, tentativas e evidências lidos do banco; sem workflow_id lista os abertos",
        "parameters": {
            "workflow_id": {"type": "int", "description": "ID do workflow", "required": False},
            "include_results": {"type": "bool", "description": "Inclui handoffs das tasks concluídas", "required": False},
        },
    },
    "workflows_control": {
        "name": "workflows_control",
        "description": "Pausa ou retoma um workflow",
        "parameters": {
            "workflow_id": {"type": "int", "description": "ID do workflow", "required": True},
            "action": {"type": "str", "description": "pause|resume", "required": True},
        },
    },
    "workflows_replan": {
        "name": "workflows_replan",
        "description": "Nova versão do plano (lista completa de tasks); preserva o que continua válido",
        "parameters": {
            "workflow_id": {"type": "int", "description": "ID do workflow", "required": True},
            "base_version": {"type": "int", "description": "Versão vigente em que o novo plano se baseia", "required": True},
            "objective": {"type": "str", "description": "Objetivo global", "required": True},
            "work_items": {"type": "list", "description": "Lista completa de tasks da nova versão", "required": True},
            "global_criteria": {"type": "list", "description": "Critérios globais", "required": False},
            "constraints": {"type": "list", "description": "Restrições e escopo", "required": False},
        },
    },
    "workflows_cancel": {
        "name": "workflows_cancel",
        "description": "Cancela o workflow, suas tasks e as execuções em curso (só as dele)",
        "parameters": {
            "workflow_id": {"type": "int", "description": "ID do workflow", "required": True},
            "reason": {"type": "str", "description": "Motivo do cancelamento", "required": False},
        },
    },
    "workflows_finish": {
        "name": "workflows_finish",
        "description": "Conclui explicitamente: completed (todas as tasks aceitas), blocked ou failed, com síntese",
        "parameters": {
            "workflow_id": {"type": "int", "description": "ID do workflow", "required": True},
            "outcome": {"type": "str", "description": "completed|blocked|failed", "required": True},
            "summary": {"type": "str", "description": "Síntese final fundamentada em evidências", "required": True},
        },
    },
    "tasks_cancel": {
        "name": "tasks_cancel",
        "description": "Cancela uma delegação em execução (apenas quem delegou, ou o humano)",
        "parameters": {
            "task_id": {"type": "int", "description": "Task de delegação a cancelar", "required": True},
            "reason": {"type": "str", "description": "Motivo do cancelamento", "required": False},
        },
    },
    "tasks_list": {
        "name": "tasks_list",
        "description": "Lista tarefas, mais recentes primeiro; sem filtros, as do job atual. Pagina com limit/offset",
        "parameters": {
            "job_id": {"type": "int", "description": "Filtrar por job ID", "required": False},
            "status": {"type": "str",
                       "description": "pending|in_progress|completed|failed|cancelled|proposed|approved|rejected",
                       "required": False},
            "limit": {"type": "int", "description": "Itens por página (padrão 20)", "required": False},
            "offset": {"type": "int", "description": "Itens a pular; use o next_offset da página anterior",
                       "required": False},
        },
    },
    "jobs_list": {
        "name": "jobs_list",
        "description": "Lista jobs do mais recente ao mais antigo. Pagina com limit/offset",
        "parameters": {
            "status": {"type": "str", "description": "planning|active|completed|failed", "required": False},
            "created_by": {"type": "str", "description": "Filtrar por criador", "required": False},
            "limit": {"type": "int", "description": "Itens por página (padrão 20)", "required": False},
            "offset": {"type": "int", "description": "Itens a pular; use o next_offset da página anterior",
                       "required": False},
        },
    },
    "jobs_get": {
        "name": "jobs_get",
        "description": "Consulta detalhes de um job específico. O job_id pode ser omitido se a variável de ambiente QUIMERA_CURRENT_JOB_ID estiver definida.",
        "parameters": {
            "job_id": {"type": "int", "description": "ID do job (opcional se QUIMERA_CURRENT_JOB_ID definida)",
                       "required": False}
        },
    },
}


def build_tools_prompt() -> str:
    """Renderiza apenas dados dinâmicos das ferramentas disponíveis."""
    lines = []
    for tool in TOOL_SCHEMA.values():
        params = ", ".join(f"{k}: {v['type']}" for k, v in tool["parameters"].items())
        line = f"- {tool['name']}"
        if params:
            line += f": {params}"
        if tool.get("description"):
            line += f" — {tool['description']}"
        lines.append(line)
    return "\n".join(lines)
