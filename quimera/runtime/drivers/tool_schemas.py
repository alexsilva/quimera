"""
Definições de ferramentas no formato OpenAI tool calling schema.
Espelham as ferramentas registradas em ToolExecutor._register_builtin_tools().
"""

from collections.abc import Iterable

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Lista arquivos e diretórios em um caminho dentro do workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Caminho relativo dentro do workspace. Use '.' para o raiz.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Lê o conteúdo de um arquivo dentro do workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Caminho relativo do arquivo a ser lido.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Escreve conteúdo em um arquivo dentro do workspace. "
                "Para sobrescrever arquivo existente por completo, envie replace_existing=true. "
                "Para mudanças parciais em arquivo existente, prefira apply_patch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Caminho relativo do arquivo a ser escrito.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Conteúdo a ser escrito no arquivo.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append", "create"],
                        "description": "Modo de escrita: overwrite (padrão), append, create (falha se já existe).",
                    },
                    "replace_existing": {
                        "type": "boolean",
                        "description": (
                            "Obrigatório para sobrescrever um arquivo já existente por completo. "
                            "Para mudanças parciais, use apply_patch."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Aplica um patch textual estruturado no workspace. "
                "Prefira esta ferramenta para alterações parciais em arquivos existentes. "
                "Use o formato nativo do Quimera com linhas como "
                "'*** Begin Patch', '*** Update File: caminho', '@@', "
                "linhas iniciadas por espaço/+/-, e '*** End Patch'. "
                "Não use cabeçalhos de diff git/unified como '---', '+++' ou 'diff --git'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": (
                            "Patch no formato:"
                            " *** Begin Patch ... *** End Patch. "
                            "Suporta Add File, Delete File e Update File."
                        ),
                    }
                },
                "required": ["patch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Busca um padrão de texto em arquivos dentro do workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Padrão de texto a buscar.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Caminho relativo onde buscar. Use '.' para todo o workspace.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_file",
            "description": "Remove um arquivo dentro do workspace. Exige dry_run=False para confirmar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Caminho relativo do arquivo a ser removido.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Padrão true (seguro). Passe false explicitamente para realmente remover.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Executa um comando shell no diretório do workspace. "
                "Use para inspeção ou validação objetiva, não para substituir ferramentas específicas."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Comando shell a executar.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": (
                "Executa um comando shell com suporte a sessão persistente, stdout/stderr incremental "
                "e polling posterior via write_stdin."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "description": "Comando shell a executar.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Diretório relativo ao workspace onde o comando deve rodar.",
                    },
                    "yield_time_ms": {
                        "type": "integer",
                        "description": "Tempo em milissegundos para esperar por saída antes de retornar.",
                    },
                    "shell": {
                        "type": "string",
                        "description": "Shell a usar para executar o comando. Padrão: shell do ambiente.",
                    },
                    "login": {
                        "type": "boolean",
                        "description": "Usa modo login do shell (-lc) quando true.",
                    },
                    "tty": {
                        "type": "boolean",
                        "description": "Executa o comando em um PTY simplificado quando true.",
                    },
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_command_session",
            "description": "Fecha explicitamente uma sessão aberta por exec_command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "integer",
                        "description": "ID da sessão a fechar.",
                    },
                    "terminate": {
                        "type": "boolean",
                        "description": "Tenta terminar o processo antes de remover a sessão.",
                    },
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_stdin",
            "description": (
                "Escreve no stdin de uma sessão aberta por exec_command ou apenas faz polling quando chars "
                "for vazio."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "integer",
                        "description": "ID da sessão retornada por exec_command.",
                    },
                    "chars": {
                        "type": "string",
                        "description": "Texto a enviar para o stdin. Use string vazia para apenas consultar a saída.",
                    },
                    "yield_time_ms": {
                        "type": "integer",
                        "description": "Tempo em milissegundos para esperar por nova saída.",
                    },
                    "close_stdin": {
                        "type": "boolean",
                        "description": "Fecha o stdin da sessão após enviar chars.",
                    },
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "Lista tarefas com filtros opcionais do job atual ou de qualquer job.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "Filtrar por job ID.",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filtrar por status, como proposed, approved, in_progress, completed, failed ou rejected.",
                    },
                    "assigned_to": {
                        "type": "string",
                        "description": "Filtrar por agente atribuído.",
                    },
                    "id": {
                        "type": "integer",
                        "description": "Filtrar por ID da task.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_jobs",
            "description": "Lista jobs de sessão disponíveis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filtrar por status, como planning, active, completed ou failed.",
                    },
                    "created_by": {
                        "type": "string",
                        "description": "Filtrar por criador do job.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job",
            "description": "Obtém detalhes de um job específico.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "ID do job a consultar. Pode ser omitido se QUIMERA_CURRENT_JOB_ID estiver definido.",
                    }
                },
                "required": [],
            },
        },
    },
]

_TASK_TOOL_NAMES = {"list_tasks", "list_jobs", "get_job"}


def resolve_tool_schemas(tool_executor=None) -> list[dict]:
    """Retorna apenas schemas coerentes com o executor/configuração atual."""
    schemas = list(TOOL_SCHEMAS)
    if tool_executor is None:
        return schemas

    registry = getattr(tool_executor, "registry", None)
    if registry is not None and hasattr(registry, "names"):
        registry_names = registry.names()
        if isinstance(registry_names, Iterable) and not isinstance(registry_names, (str, bytes, dict)):
            enabled_names = set(registry_names)
            schemas = [schema for schema in schemas if schema["function"]["name"] in enabled_names]

    config = getattr(tool_executor, "config", None)
    if config is not None and getattr(config, "db_path", None) is None:
        schemas = [
            schema for schema in schemas
            if schema["function"]["name"] not in _TASK_TOOL_NAMES
        ]

    policy = getattr(tool_executor, "policy", None)
    blocked_tools = getattr(policy, "blocked_tools", None)
    if blocked_tools:
        blocked_names = set(blocked_tools)
        schemas = [
            schema for schema in schemas
            if schema["function"]["name"] not in blocked_names
        ]

    return schemas
