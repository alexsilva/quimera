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
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean", "description": "Success status"},
                    "content": {"type": "string", "description": "File/directory names separated by newlines. Directories have trailing '/'"},
                    "error": {"type": "string", "description": "Error message if ok=false"},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Lê o conteúdo de um arquivo dentro do workspace. Opcionalmente, especifique start_line e end_line para ler apenas um intervalo de linhas (1-indexed).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Caminho relativo do arquivo a ser lido.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Primeira linha a ler (1-indexed, inclusiva). Opcional.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Última linha a ler (1-indexed, inclusiva). Se omitido e start_line fornecido, lê até o fim. Opcional.",
                    }
                },
                "required": ["path"]
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "File content (or requested range)"},
                    "truncated": {"type": "boolean", "description": "Content was truncated if true"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
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
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "Confirmation message"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
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
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "Success message with changed files"},
                    "data": {"type": "object", "properties": {"changed_files": {"type": "array", "items": {"type": "string"}}}, "description": "List of changed files"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
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
                        "description": "Substring literal a buscar (não suporta regex).",
                    },
                    "path": {
                        "type": "string",
                        "description": "Caminho relativo onde buscar. Use '.' para todo o workspace.",
                    },
                    "include_glob": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Filtro glob opcional para paths retornados, como '*.py' ou 'quimera/**/*.py'.",
                    },
                    "exclude_dirs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Diretórios adicionais a ignorar durante a busca.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Limite opcional de resultados, limitado pelo máximo global da runtime.",
                    },
                },
                "required": ["pattern"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "Results in path:line:content format, one per line"},
                    "truncated": {"type": "boolean"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
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
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "Dry-run or removal confirmation message"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
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
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean", "description": "True if exit_code=0"},
                    "content": {"type": "string", "description": "stdout (and optionally stderr) output"},
                    "exit_code": {"type": "integer", "description": "Process exit code"},
                    "truncated": {"type": "boolean"},
                    "duration_ms": {"type": "integer"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content", "exit_code"],
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
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "Session status and output"},
                    "exit_code": {"type": "integer", "description": "None if still running"},
                    "session_id": {"type": "integer", "description": "Persistent session ID"},
                    "truncated": {"type": "boolean"},
                    "duration_ms": {"type": "integer"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
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
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "Closure confirmation with status"},
                    "exit_code": {"type": "integer"},
                    "session_id": {"type": "integer"},
                    "duration_ms": {"type": "integer"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
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
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "Session output after write/poll"},
                    "exit_code": {"type": "integer"},
                    "session_id": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
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
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "JSON array of TaskRecord objects"},
                    "truncated": {"type": "boolean"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
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
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "JSON array of JobRecord objects"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
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
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "JSON of the job object, or 'null'"},
                    "data": {"type": "object", "properties": {"job": {"type": "object"}}, "description": "Job details"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Pesquisa na internet usando DuckDuckGo Lite. Retorna uma lista de resultados (título, URL, trecho).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Termo de busca na internet.",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Número máximo de resultados a retornar (padrão: 5).",
                    },
                },
                "required": ["query"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "Markdown links with snippets"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Faz uma requisição HTTP GET a uma URL e extrai o conteúdo textual. Útil para ler páginas web retornadas por web_search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL completa (incluindo https://) da página a ser lida.",
                    },
                    "raw": {
                        "type": "boolean",
                        "description": "Se true, retorna o HTML bruto sem extrair texto. Padrão: false.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Tempo máximo de espera pela resposta em segundos. Padrão: 30.",
                    },
                },
                "required": ["url"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "Extracted page text or raw HTML"},
                    "error": {"type": "string", "description": "Network error details if ok=false"},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": "Cria ou atualiza itens de TODO session-scoped. Mantém exatamente um in_progress por job.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "Lista de itens TODO para criar ou atualizar. Cada item deve conter 'content' e pode opcionalmente incluir 'id' para atualização, 'status' e 'priority'.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "integer",
                                    "description": "ID do item existente para atualização (opcional).",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "Descrição da tarefa.",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "done", "cancelled"],
                                    "description": "Status do item. 'in_progress' move qualquer outro in_progress para pending.",
                                },
                                "priority": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                    "description": "Prioridade do item.",
                                },
                            },
                            "required": ["content"],
                        },
                    },
                    "agent": {
                        "type": "string",
                        "description": "Nome do agente (opcional, auto-detectado se omitido).",
                    },
                },
                "required": ["todos"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "JSON array of updated TodoItem objects"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_list",
            "description": "Lista todos os itens TODO da sessão atual.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "JSON array of TodoItem objects"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "Salva ou atualiza uma entrada determinística de memória estruturada do workspace, sem expor o arquivo interno.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Namespace lógico da memória, ex: workspace, decisions, handoff.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Chave determinística dentro do namespace.",
                    },
                    "value": {
                        "description": "Valor JSON-serializable a persistir.",
                    },
                    "ttl_seconds": {
                        "type": ["integer", "null"],
                        "description": "TTL opcional em segundos. Se omitido ou null, a entrada não expira.",
                    },
                },
                "required": ["namespace", "key", "value"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "revision": {"type": "integer"},
                    "namespace": {"type": "string"},
                    "key": {"type": "string"},
                    "updated_at": {"type": "string"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "revision", "namespace", "key", "updated_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_retrieve",
            "description": "Recupera memória estruturada do workspace por namespace, key, prefixo ou tags, sem busca semântica.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Filtra por namespace exato.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Busca exata por key.",
                    },
                    "prefix": {
                        "type": "string",
                        "description": "Filtra keys que começam com este prefixo.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filtra entradas que contenham todas as tags fornecidas.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Máximo de entradas retornadas.",
                    },
                },
                "required": [],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "revision": {"type": "integer"},
                    "entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "namespace": {"type": "string"},
                                "key": {"type": "string"},
                                "value": {},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "created_at": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                                "created_by": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                                "updated_at": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                                "updated_by": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                                "ttl_seconds_remaining": {"oneOf": [{"type": "integer"}, {"type": "null"}]},
                            },
                            "required": ["namespace", "key", "value", "tags", "updated_at", "updated_by", "ttl_seconds_remaining"],
                        },
                    },
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "revision", "entries"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": (
                "Delega uma tarefa para outro agente do Quimera. "
                "Use quando precisar de especialidade específica "
                "(ex: codex para codificação, claude para revisão, "
                "gemini para arquitetura, opencode-big-pickle para edição)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_agent": {
                        "type": "string",
                        "description": "Nome do agente alvo (ex: codex, claude, gemini, opencode-big-pickle).",
                    },
                    "request": {
                        "type": "string",
                        "description": "Descrição clara do que o agente deve fazer.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Contexto adicional relevante (opcional).",
                    },
                    "fallback_agents": {
                        "type": "array",
                        "description": (
                            "Lista opcional de agentes de fallback, tentados em sequência "
                            "se o agente alvo principal estiver indisponível/sem resposta."
                        ),
                        "items": {"type": "string"},
                    },
                    "steps": {
                        "type": "array",
                        "description": (
                            "Passos adicionais opcionais executados em sequência. "
                            "Cada item deve conter target_agent, request e context opcional."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "target_agent": {"type": "string"},
                                "request": {"type": "string"},
                                "context": {"type": "string"},
                                "fallback_agents": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["target_agent", "request"],
                        },
                    },
                },
                "required": ["target_agent", "request"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "Concatenated agent responses. Multi-step prefixed with '[agent_name] response'."},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    # ------------------------------------------------------------------
    # Git tools
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": (
                "Retorna o status do repositório git de forma estruturada: branch atual, "
                "arquivos staged/unstaged/untracked, e distância ao upstream (ahead/behind)."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "branch": {"type": "string"},
                            "staged": {"type": "array", "items": {"type": "object"}},
                            "unstaged": {"type": "array", "items": {"type": "object"}},
                            "untracked": {"type": "array", "items": {"type": "string"}},
                            "ahead": {"type": "integer"},
                            "behind": {"type": "integer"},
                            "clean": {"type": "boolean"},
                        },
                    },
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Lista commits recentes de forma estruturada (hash, autor, data, mensagem).",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_count": {
                        "type": "integer",
                        "description": "Número máximo de commits a retornar (padrão: 10, máx: 200).",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch ou ref a partir da qual listar commits. Padrão: HEAD.",
                    },
                },
                "required": [],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "commits": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "hash": {"type": "string"},
                                        "short_hash": {"type": "string"},
                                        "author": {"type": "string"},
                                        "author_email": {"type": "string"},
                                        "date": {"type": "string"},
                                        "message": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": (
                "Retorna o diff do repositório. Pode mostrar alterações não staged (padrão), "
                "staged (staged=true) ou entre dois refs (ref1, ref2). "
                "Filtro opcional por arquivo (path)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "staged": {
                        "type": "boolean",
                        "description": "Se true, mostra diff staged (git diff --staged). Padrão: false.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Caminho relativo ao workspace para filtrar o diff.",
                    },
                    "ref1": {
                        "type": "string",
                        "description": "Ref/commit base para comparação (ex: HEAD~1, main).",
                    },
                    "ref2": {
                        "type": "string",
                        "description": "Ref/commit alvo para comparação. Requer ref1.",
                    },
                },
                "required": [],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "stat + diff completo"},
                    "truncated": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "diff": {"type": "string"},
                            "stat": {"type": "string"},
                            "staged": {"type": "boolean"},
                        },
                    },
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_branch",
            "description": "Lista branches locais (e opcionalmente remotas) do repositório.",
            "parameters": {
                "type": "object",
                "properties": {
                    "all": {
                        "type": "boolean",
                        "description": "Lista branches locais e remotas (git branch --all). Padrão: false.",
                    },
                    "remote": {
                        "type": "boolean",
                        "description": "Lista apenas branches remotas (git branch --remote). Padrão: false.",
                    },
                },
                "required": [],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "branches": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "upstream": {"type": "string"},
                                        "current": {"type": "boolean"},
                                    },
                                },
                            },
                            "current": {"type": "string"},
                        },
                    },
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_fetch",
            "description": (
                "Faz fetch de um remote, atualizando refs remotas locais sem alterar "
                "branches locais nem o working tree."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "remote": {
                        "type": "string",
                        "description": "Nome do remote (padrão: origin).",
                    },
                    "prune": {
                        "type": "boolean",
                        "description": "Remove refs remotas que não existem mais no remote (--prune). Padrão: false.",
                    },
                },
                "required": [],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "remote": {"type": "string"},
                            "output": {"type": "string"},
                        },
                    },
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_add",
            "description": "Adiciona arquivos ao índice (staging area). Equivale a `git add <paths>`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Arquivo(s) a adicionar. Use '.' para adicionar tudo. Padrão: '.'.",
                    },
                },
                "required": [],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "paths": {"type": "array", "items": {"type": "string"}},
                            "staged": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Cria um commit com os arquivos staged e a mensagem fornecida.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Mensagem do commit (obrigatória).",
                    },
                    "amend": {
                        "type": "boolean",
                        "description": "Emendar o commit anterior (--amend). Padrão: false.",
                    },
                },
                "required": ["message"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "commit": {"type": "string", "description": "Full commit hash"},
                            "short_hash": {"type": "string"},
                            "message": {"type": "string"},
                        },
                    },
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_checkout",
            "description": (
                "Muda para uma branch existente ou cria uma nova (create=true). "
                "Force-push e mudanças destrutivas devem ser feitas via run_shell com aprovação explícita."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "Nome da branch de destino (obrigatório).",
                    },
                    "create": {
                        "type": "boolean",
                        "description": "Cria a branch antes de mudar (git checkout -b). Padrão: false.",
                    },
                },
                "required": ["branch"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "branch": {"type": "string"},
                            "created": {"type": "boolean"},
                            "output": {"type": "string"},
                        },
                    },
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_push",
            "description": (
                "Faz push para um remote. "
                "Force-push (--force / -f) é bloqueado; use run_shell com aprovação explícita se necessário."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "remote": {
                        "type": "string",
                        "description": "Nome do remote (padrão: origin).",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch local a enviar. Se omitido, usa a branch atual.",
                    },
                    "set_upstream": {
                        "type": "boolean",
                        "description": "Define o upstream da branch local (-u). Padrão: false.",
                    },
                },
                "required": [],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "remote": {"type": "string"},
                            "branch": {"type": "string"},
                            "output": {"type": "string"},
                        },
                    },
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": "Lista os agentes ativos na sessão atual do chat. A lista reflete o pool atual — agentes que falharam ou saíram não aparecem.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "JSON array of active agent names"},
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Faz uma pergunta ao usuário humano e aguarda a resposta. "
                "Modo padrão: pergunta aberta de texto livre (só 'question') — use para "
                "pedir informação, esclarecimento ou instrução que só o humano tem. "
                "Modo enquete (opcional): forneça 'options' (>=2) para oferecer escolhas; "
                "o usuário escolhe pelo número ou pelo texto da opção. "
                "Bloqueia até o usuário responder."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Pergunta clara a exibir ao usuário.",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "description": (
                            "Opcional. Lista de opções (mínimo 2) para uma enquete. "
                            "Omita para uma pergunta aberta de texto livre."
                        ),
                    },
                },
                "required": ["question"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "content": {"type": "string", "description": "Resposta do usuário (texto livre ou opção escolhida)"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer", "description": "Índice 0-based da opção escolhida, ou -1 para texto livre"},
                            "value": {"type": "string", "description": "Texto da resposta do usuário"},
                        },
                    },
                    "error": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["ok", "content"],
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

    is_delegate_available = getattr(tool_executor, "is_delegate_available", None)
    if callable(is_delegate_available) and not is_delegate_available():
        schemas = [
            schema for schema in schemas
            if schema["function"]["name"] not in ("delegate", "list_agents")
        ]

    is_ask_user_available = getattr(tool_executor, "is_ask_user_available", None)
    if callable(is_ask_user_available) and not is_ask_user_available():
        schemas = [
            schema for schema in schemas
            if schema["function"]["name"] != "ask_user"
        ]

    return schemas
