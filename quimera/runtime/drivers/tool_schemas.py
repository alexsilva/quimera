"""
Definições de ferramentas no formato OpenAI tool calling schema.
Espelham as ferramentas registradas em ToolExecutor._register_builtin_tools().
"""

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
            "description": "Escreve conteúdo em um arquivo dentro do workspace.",
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
                },
                "required": ["path", "content"],
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
            "description": "Executa um comando shell no diretório do workspace.",
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
            "description": "Lista tarefas do job atual com filtros opcionais.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filtrar por status: pending, in_progress, completed, failed.",
                    },
                    "assigned_to": {
                        "type": "string",
                        "description": "Filtrar por agente atribuído.",
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
                "properties": {},
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
                        "description": "ID do job a consultar.",
                    }
                },
                "required": ["job_id"],
            },
        },
    },
]
