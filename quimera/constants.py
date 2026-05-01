"""Componentes de `quimera.constants`."""
import enum
import os


class Visibility(str, enum.Enum):
    """Nível de visibilidade da execução do agente."""
    QUIET = "quiet"
    SUMMARY = "summary"
    FULL = "full"

MAX_STDERR_LINES = 5
_env_limit = os.getenv("QUIMERA_MAX_STDERR_LINES")
if _env_limit is not None:
    try:
        MAX_STDERR_LINES = int(_env_limit)
    except Exception:
        pass

DEFAULT_FIRST_AGENT = "claude"
INPUT_PROMPT = "Você: "

# Protocol markers
EXTEND_MARKER = "[DEBATE]"
NEEDS_INPUT_MARKER = "[NEEDS_INPUT]"
ROUTE_PREFIX = "[ROUTE:"
STATE_UPDATE_START = "[STATE_UPDATE]"
STATE_UPDATE_END = "[/STATE_UPDATE]"

# Commands
CMD_EXIT = "/exit"
CMD_CLEAR = "/clear"
CMD_PROMPT = "/prompt"
CMD_HELP = "/help"
CMD_AGENTS = "/agents"
CMD_CONNECT = "/connect"
CMD_RELOAD = "/reload"
CMD_CONTEXT = "/context"
CMD_CONTEXT_EDIT = "/context-edit"
CMD_EDIT = "/edit"
CMD_FILE_PREFIX = "/file"
CMD_TASK = "/task"
CMD_RESET_STATE = "/reset-state"
CMD_APPROVE = "/approve"
CMD_APPROVE_ALL = "/approve-all"
CMD_ALIASES = {"/e": CMD_EDIT, "/r": CMD_CONTEXT, "/g": CMD_HELP, "/y": CMD_APPROVE, "/a": CMD_APPROVE, "/aa": CMD_APPROVE_ALL}
USER_ROLE = "human"

# Messages
MSG_CHAT_STARTED = "Chat multi-agente iniciado (/exit para sair)\n"
MSG_SESSION_LOG = "Log da sessão:\n  {}\n"
MSG_SESSION_STATUS = (
    "Sessão {session_id} | histórico restaurado: {history_count} mensagem(ns) | "
    "resumo carregado: {summary_loaded}\n"
)
MSG_MIGRATION = "[migração] {}\n"
MSG_MEMORY_SAVING = "\n[memória] histórico salvo. Gerando resumo da sessão..."
MSG_MEMORY_FAILED = "[memória] não foi possível gerar o resumo.\n"
MSG_SHUTDOWN = "\nEncerrando chat."
MSG_DOUBLE_PREFIX = "\nUse apenas um prefixo por vez: /claude ou /codex\n"
MSG_EMPTY_INPUT = "\nUse /{} <mensagem>\n"
HANDOFF_SYNTHESIS_MSG = (
    "Você delegou a seguinte subtarefa ao {agent}:\n\n{task}\n\n"
    "Resposta do {agent} à sua delegação:\n\n{response}\n\n"
    "Sintetize uma resposta final para o humano que integre sua análise com a resposta do {agent}. "
    "NÃO repita a resposta do {agent} — incorpore-a na sua conclusão. Avance o diálogo.\n"
    "Se a resposta do {agent} foi incompleta ou inesperada, indique isso ao humano e sugira o próximo passo.\n"
    "Ao finalizar, indique explicitamente o próximo passo: continuar com outra tarefa, pedir input humano, ou finalizar.\n"
    "Se a tarefa estiver completa, diga isso explicitamente em vez de deixar em aberto.\n"
)

## Tools Schema
TOOL_SCHEMA = {
    "list_files": {
        "name": "list_files",
        "description": "Lista arquivos e diretórios em um caminho específico",
        "parameters": {
            "path": {"type": "str", "description": "Caminho do diretório", "required": True}
        },
        "example": '<tool function="list_files" path="." />'
    },
    "read_file": {
        "name": "read_file",
        "description": "Lê o conteúdo completo de um arquivo",
        "parameters": {
            "path": {"type": "str", "description": "Caminho absoluto do arquivo", "required": True}
        },
        "example": '<tool function="read_file" path="/src/app.py" />'
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
        "example": '<tool function="write_file">{"path":"/src/new.py","content":"print(\\"hello\\")"}</tool>'
    },
    "apply_patch": {
        "name": "apply_patch",
        "description": "Aplica um patch textual estruturado. Ferramenta preferida para alterações parciais em arquivos existentes",
        "parameters": {
            "patch": {"type": "str", "description": "Patch no formato *** Begin Patch ... *** End Patch",
                      "required": True}
        },
        "example": '<tool function="apply_patch">{"patch":"*** Begin Patch\\n*** Update File: /src/app.py\\n@@\\n-old\\n+new\\n*** End Patch"}</tool>'
    },
    "grep_search": {
        "name": "grep_search",
        "description": "Busca um padrão em arquivos de um diretório",
        "parameters": {
            "pattern": {"type": "str", "description": "Regex a buscar", "required": True},
            "path": {"type": "str", "description": "Diretório base", "required": False},
        },
        "example": '<tool function="grep_search" pattern="class User" path="/src" />'
    },
    "run_shell": {
        "name": "run_shell",
        "description": "Executa um comando no terminal",
        "parameters": {
            "command": {"type": "str", "description": "Comando shell", "required": True}
        },
        "example": '<tool function="run_shell" command="git status" />'
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
        "example": '<tool function="exec_command">{"cmd":"python -i","tty":true}</tool>'
    },
    "write_stdin": {
        "name": "write_stdin",
        "description": "Envia texto ao stdin de uma sessão aberta por exec_command ou faz polling",
        "parameters": {
            "session_id": {"type": "int", "description": "ID retornado por exec_command", "required": True},
            "chars": {"type": "str", "description": "Texto a enviar; vazio faz apenas polling", "required": False},
            "yield_time_ms": {"type": "int", "description": "Espera por nova saída", "required": False},
        },
        "example": '<tool function="write_stdin">{"session_id":7,"chars":"","yield_time_ms":1000}</tool>'
    },
    "close_command_session": {
        "name": "close_command_session",
        "description": "Fecha explicitamente uma sessão aberta por exec_command",
        "parameters": {
            "session_id": {"type": "int", "description": "ID da sessão", "required": True},
        },
        "example": '<tool function="close_command_session" session_id="7" />'
    },
    "list_tasks": {
        "name": "list_tasks",
        "description": "Lista tarefas de um job ou todas",
        "parameters": {
            "job_id": {"type": "int", "description": "Filtrar por job ID", "required": False},
            "status": {"type": "str", "description": "pending|in_progress|completed|failed|proposed|approved|rejected",
                       "required": False},
        },
        "example": '<tool function="list_tasks" job_id="1" status="approved" />'
    },
    "list_jobs": {
        "name": "list_jobs",
        "description": "Lista todos os jobs ativos",
        "parameters": {
            "status": {"type": "str", "description": "planning|active|completed|failed", "required": False},
            "created_by": {"type": "str", "description": "Filtrar por criador", "required": False},
        },
        "example": '<tool function="list_jobs" status="planning" />'
    },
    "get_job": {
        "name": "get_job",
        "description": "Consulta detalhes de um job específico. O job_id pode ser omitido se a variável de ambiente QUIMERA_CURRENT_JOB_ID estiver definida.",
        "parameters": {
            "job_id": {"type": "int", "description": "ID do job (opcional se QUIMERA_CURRENT_JOB_ID definida)",
                       "required": False}
        },
        "example": '<tool function="get_job" />'
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
        if tool.get("example"):
            line += f" | exemplo: {tool['example']}"
        lines.append(line)
    return "\n".join(lines)


def build_help(agent_names):
    """Monta help."""
    help_text = (
            "\nComandos:\n" +
            "- /task <descrição>: cria uma task explícita do humano e roteia para o melhor agente\n"
            "- /planning <mensagem>: modo planejamento — workspace somente leitura, sem edição de arquivos\n"
            "- /analysis <mensagem>: modo análise — somente leitura, sem edição de arquivos\n"
            "- /design <mensagem>: modo design — arquitetura e design sem execução\n"
            "- /review <mensagem>: modo revisão — somente revisão de código, sem edições\n"
            "- /execute <mensagem>: modo execução — acesso completo a ferramentas e remove restrições do modo anterior\n"
            "- /agents: lista os agentes disponíveis\n"
            "- /connect <agente>: configura interativamente a conexão de um agente e persiste no base_dir\n"
            "- /clear: limpa a tela do terminal\n"
            "- /prompt [agente]: simula o prompt final e mostra análise dos blocos\n"
            "- /context: mostra o contexto atual\n"
            "- /context-edit: abre o contexto persistente no editor ($EDITOR, ou nano/vim/vi como fallback)\n"
            "- /edit: abre o editor ($EDITOR, ou nano/vim/vi como fallback) para compor uma mensagem longa\n"
            "- /file <caminho>: usa o conteúdo de um arquivo como mensagem\n"
            "- /reset-state: limpa o shared_state (objetivo, passo, critérios) sem apagar o histórico\n"
            "- /approve: pré-aprova a próxima chamada de ferramenta\n"
            "- /approve-all: aprova automaticamente todas as chamadas de ferramenta seguintes\n"
            "- /help: mostra esta ajuda\n"
            "- /exit: encerra a sessão\n"
    )
    return help_text


def build_agents_help(agent_names):
    """Monta a lista de agentes disponíveis."""
    agents = "\n".join(f"- /{name} <mensagem>: {name.capitalize()} responde" for name in agent_names)
    return "\nAgentes:\n" + (agents if agents else "- nenhum")


# Shared state keys that should be trimmed when building prompts
_SHARED_STATE_TRIM_KEYS = [
    "goal_canonical", "current_step", "acceptance_criteria", "allowed_scope",
    "non_goals", "out_of_scope_notes", "next_step", "task_overview",
]
