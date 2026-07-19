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
        "description": "Cria um arquivo novo ou reescreve um arquivo inteiro quando isso for realmente necessário",
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
        "description": "Aplica um patch textual estruturado. Ferramenta preferida para alterações parciais em arquivos existentes",
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
    "tasks": {
        "name": "tasks",
        "description": "Cria uma task como o comando /task e retorna dados para acompanhamento",
        "parameters": {
            "description": {"type": "str", "description": "Descrição da task", "required": True},
        },
    },
    "list_tasks": {
        "name": "list_tasks",
        "description": "Lista tarefas de um job ou todas",
        "parameters": {
            "job_id": {"type": "int", "description": "Filtrar por job ID", "required": False},
            "status": {"type": "str", "description": "pending|in_progress|completed|failed|proposed|approved|rejected",
                       "required": False},
        },
    },
    "list_jobs": {
        "name": "list_jobs",
        "description": "Lista todos os jobs ativos",
        "parameters": {
            "status": {"type": "str", "description": "planning|active|completed|failed", "required": False},
            "created_by": {"type": "str", "description": "Filtrar por criador", "required": False},
        },
    },
    "get_job": {
        "name": "get_job",
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
