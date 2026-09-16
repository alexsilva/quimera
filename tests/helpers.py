"""Helpers compartilhados entre módulos de teste do Quimera.

Funções utilitárias reutilizáveis (não fixtures) que evitam duplicação de
setup idêntico entre múltiplos arquivos de teste.
"""

from unittest.mock import MagicMock

from quimera.runtime.models import ToolResult


def task_row(task_id, db_path):
    """Lê a linha atual de uma task no SQLite (status e metadados)."""
    from quimera.tasks import api as tasks_api

    conn = tasks_api.get_conn(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT status, assigned_to, result, notes, reviewed_by, failed_agents, attempt_count FROM tasks WHERE id = ?",
        (task_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def make_policy(config):
    """Cria ToolPolicy com todos os validators registrados."""
    from quimera.runtime.policy import ToolPolicy
    from quimera.runtime.registry import ToolRegistry
    from quimera.runtime.tools import delegate as delegate_module
    from quimera.runtime.tools import files as files_tools
    from quimera.runtime.tools import memory as memory_tools
    from quimera.runtime.tools import patch as patch_tools
    from quimera.runtime.tools import tasks as tasks_tools
    from quimera.runtime.tools import todo as todo_tools

    policy = ToolPolicy(config)
    registry = ToolRegistry()
    files_tools.register(registry, policy, config)
    patch_tools.register(registry, policy, config)
    tasks_tools.register(registry, policy, config)
    todo_tools.register(registry, policy, config)
    memory_tools.register(registry, policy, config)
    delegate_module.register(registry, policy, config)
    return policy


def make_mcp_executor(tool_names=None, call_result=None):
    """Cria um mock de ToolExecutor com registry e execute configurados."""
    executor = MagicMock()
    names = tool_names or ["read_file", "run_shell"]
    executor.registry.names.return_value = names
    executor.config.workspace = None
    executor.policy.blocked_tools = set()
    if call_result is None:
        call_result = ToolResult(ok=True, tool_name="read_file", content="ok")
    executor.execute.return_value = call_result
    return executor


def make_mcp_server(executor=None):
    """Cria um MCPServer com executor padrão (ou fornecido)."""
    from quimera.runtime.mcp import MCPServer

    return MCPServer(executor or make_mcp_executor())
