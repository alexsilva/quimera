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

    return schemas
