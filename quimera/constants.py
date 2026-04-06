EXTEND_MARKER = "[DEBATE]"
NEEDS_INPUT_MARKER = "[NEEDS_INPUT]"
ROUTE_PREFIX = "[ROUTE:"
STATE_UPDATE_START = "[STATE_UPDATE]"
STATE_UPDATE_END = "[/STATE_UPDATE]"
STATE_UPDATE_EXAMPLE = (
    '{\n'
    '  "goal": "objetivo atual da conversa",\n'
    '  "decisions": ["o que foi aceito"],\n'
    '  "open_disagreements": ["pontos em aberto"],\n'
    '  "next_step": "ação esperada"\n'
    '}'
)

CMD_EXIT = "/exit"

# Limite de linhas de stderr exibidas em caso de falha de agente
MAX_STDERR_LINES = 5
CMD_HELP = "/help"
CMD_CONTEXT = "/context"
CMD_CONTEXT_EDIT = "/context edit"
CMD_EDIT = "/edit"
CMD_FILE_PREFIX = "/file "
CMD_TASK = "/task"

DEFAULT_FIRST_AGENT = "claude"

## Tools Schema (dynamic prompt generation)
# This schema formalizes available tooling with descriptions and parameter specs.
# It allows agents to discover how to invoke tools and what arguments are expected.
TOOL_SCHEMA = {
    "list_files": {
        "name": "list_files",
        "description": "Lista arquivos e diretórios em um caminho específico",
        "parameters": {
            "path": {"type": "str", "description": "Caminho do diretório", "required": True}
        },
        "example": 'list_files("/src")'
    },
    "read_file": {
        "name": "read_file",
        "description": "Lê o conteúdo completo de um arquivo",
        "parameters": {
            "path": {"type": "str", "description": "Caminho absoluto do arquivo", "required": True}
        },
        "example": 'read_file("/src/app.py")'
    },
    "write_file": {
        "name": "write_file",
        "description": "Cria ou sobrescreve um arquivo com o conteúdo especificado",
        "parameters": {
            "path": {"type": "str", "description": "Caminho absoluto do arquivo", "required": True},
            "content": {"type": "str", "description": "Conteúdo a escrever", "required": True},
        },
        "example": 'write_file(path="/src/new.py", content="print(\"hello\")")'
    },
    "grep_search": {
        "name": "grep_search",
        "description": "Busca um padrão em arquivos de um diretório",
        "parameters": {
            "pattern": {"type": "str", "description": "Regex a buscar", "required": True},
            "path": {"type": "str", "description": "Diretório base", "required": False},
        },
        "example": 'grep_search("class User", path="/src")'
    },
    "run_shell": {
        "name": "run_shell",
        "description": "Executa um comando no terminal",
        "parameters": {
            "command": {"type": "str", "description": "Comando shell", "required": True}
        },
        "example": 'run_shell("git status")'
    },
    "list_tasks": {
        "name": "list_tasks",
        "description": "Lista tarefas de um job ou todas",
        "parameters": {
            "job_id": {"type": "int", "description": "Filtrar por job ID", "required": False},
            "status": {"type": "str", "description": "pending|in_progress|completed|failed|proposed|approved|rejected", "required": False},
        },
        "example": 'list_tasks(job_id=1, status="approved")'
    },
    "list_jobs": {
        "name": "list_jobs",
        "description": "Lista todos os jobs ativos",
        "parameters": {
            "status": {"type": "str", "description": "planning|active|completed|failed", "required": False},
            "created_by": {"type": "str", "description": "Filtrar por criador", "required": False},
        },
        "example": 'list_jobs(status="planning")'
    },
    "get_job": {
        "name": "get_job",
        "description": "Consulta detalhes de um job específico. O job_id pode ser omitido se a variável de ambiente QUIMERA_CURRENT_JOB_ID estiver definida.",
        "parameters": {
            "job_id": {"type": "int", "description": "ID do job (opcional se QUIMERA_CURRENT_JOB_ID definida)", "required": False}
        },
        "example": 'get_job()'
    },
}

def build_tools_prompt() -> str:
    """Gera um bloco de ferramentas disponíveis a partir do TOOL_SCHEMA.

    Formato simples e previsível para que agentes consigam interpretar as tools dinamicamente.
    """
    lines = ["""
    Ferramentas disponíveis:
        - Retorno modelo formatado com dados em um JSON válido:
        ```tool 
        {"name": "<tool_name>", "arguments": {...}}
        ```
     """]
    for tool in TOOL_SCHEMA.values():
        params = ", ".join(f"{k}: {v['type']}" for k, v in tool["parameters"].items())
        lines.append(f"- {tool['name']}: {params}")
        lines.append(f"  Descrição: {tool['description']}")
        if tool.get("example"):
            lines.append(f"  Exemplo: {tool['example']}")
    return "\n".join(lines) + "\n"


USER_ROLE = "human"
INPUT_PROMPT = "Você: "

PROMPT_HEADER = "Você é {agent} em uma conversa com:\n{participants}"
PROMPT_CONTEXT = "CONTEXTO PERSISTENTE:\n{context}"
PROMPT_CONVERSATION = "CONVERSA:\n{conversation}"
PROMPT_SPEAKER = "[{agent}]:"
PROMPT_BASE_RULES = (
    "Você participa de uma conversa entre um humano e outros agentes.\n"
    "\n"
    "Prioridade:\n"
    "1. Responder ao humano de forma direta e útil.\n"
    "2. Colaborar com outros agentes apenas quando necessário.\n"
    "3. Usar protocolo interno (ROUTE, STATE_UPDATE) só quando a situação exigir.\n"
    "\n"
    "Regras:\n"
    "- Fale como em chat, com linguagem natural. Seja direto.\n"
    "- Não execute ações não autorizadas pelo humano.\n"
    "- Se faltar informação crítica, use [NEEDS_INPUT].\n"
    "- Se outro agente já cobriu parte do problema, complemente sem reiniciar.\n"
    "- Tasks novas só podem nascer quando o humano usar o comando /task.\n"
    "- Referencie arquivos como `/caminho/absoluto/arquivo:linha` em linha própria.\n"
    "- Pode discordar e comentar respostas anteriores.\n"
    "- Não descreva protocolo interno ao humano.\n"
)
PROMPT_DEBATE_RULE = (
    "- Se o tópico exigir debate mais aprofundado entre os agentes, "
    "inclua {marker} ao final da sua resposta (sem explicação). "
    "Caso contrário, não inclua nada.\n"
)
def build_route_rule(agent_names):
    return (
        "- Para delegar: [ROUTE:agente] task: <o que fazer> | context: <contexto> | expected: <formato>\n"
        "- 'task' é obrigatório. Inclua contexto suficiente — o outro agente não vê o histórico.\n"
        "- Só delegue quando houver ganho real. Se consegue fazer, faça.\n"
    )

def build_help(agent_names):
    help_text = (
        "\nComandos:\n" +
        "\n".join([f"- /{s} <mensagem>: {s.capitalize()} responde" for s in agent_names]) + "\n"
        "- /task <descrição>: cria uma task explícita do humano e roteia para o melhor agente\n"
        "- /context: mostra o contexto atual\n"
        "- /context edit: abre o contexto persistente no editor ($EDITOR, ou nano/vim/vi como fallback)\n"
        "- /edit: abre o editor ($EDITOR, ou nano/vim/vi como fallback) para compor uma mensagem longa\n"
        "- /file <caminho>: usa o conteúdo de um arquivo como mensagem\n"
        "- /help: mostra esta ajuda\n"
        "- /exit: encerra a sessão\n"
    )
    return help_text

PROMPT_SESSION_STATE = (
    "ESTADO DA SESSÃO:\n"
    "- SESSÃO ATUAL: {session_id}\n"
    "- JOB_ID ATUAL: {current_job_id}\n"
    "- NOVA SESSÃO: {is_new_session}\n"
    "- HISTÓRICO RESTAURADO: {history_restored}\n"
    "- RESUMO CARREGADO: {summary_loaded}\n"
)
PROMPT_HANDOFF = "MENSAGEM DIRETA DO OUTRO AGENTE:\n{handoff}"
HANDOFF_SYNTHESIS_MSG = (
    "Você delegou a seguinte subtarefa ao {agent}:\n\n{task}\n\n"
    "Resposta do {agent} à sua delegação:\n\n{response}\n\n"
    "Sintetize uma resposta final para o humano que integre sua análise com a resposta do {agent}. "
    "NÃO repita a resposta do {agent} — incorpore-a na sua conclusão. Avance o diálogo.\n"
    "Se a resposta do {agent} foi incompleta ou inesperada, indique isso ao humano e sugira o próximo passo.\n"
    "Ao finalizar, indique explicitamente o próximo passo: continuar com outra tarefa, pedir input humano, ou finalizar.\n"
    "Se a tarefa estiver completa, diga isso explicitamente em vez de deixar em aberto.\n"
)
PROMPT_SHARED_STATE = "ESTADO COMPARTILHADO:\n{state}"
PROMPT_AGENT_METRICS = "MÉTRICAS DO AGENTE:\n{metrics}"
PROMPT_STATE_UPDATE_RULE = (
    "- Se houver decisão nova, discordância ou mudança de objetivo, inclua ao final:\n"
    "[STATE_UPDATE]\n"
    '{"goal": "...", "decisions": [...], "open_disagreements": [...], "next_step": "..."}\n'
    "[/STATE_UPDATE]\n"
)

PROMPT_REVIEWER_RULE = (
    "- Você é o segundo agente nesta rodada. "
    "Concorde, discorde ou complemente a resposta anterior sem repeti-la. "
    "Se discordar, explique por quê e ofereça alternativa concreta.\n"
)
PROMPT_HANDOFF_RULE = (
    "- Você recebeu uma subtarefa delegada por outro agente. Responda diretamente à tarefa.\n"
    "- Inicie com [ACK:<HANDOFF_ID>] para confirmar recebimento.\n"
    "- Não delegue de volta ao agente que te chamou. Não expanda o escopo.\n"
    "- Ao final, indique o próximo passo para o agente que delegou.\n"
)

PROMPT_TOOL_RULE = (
    "- Você tem acesso às ferramentas customizadas listadas abaixo em 'Ferramentas disponíveis'.\n"
    "- Quando um participante usar o formato de bloco tool com JSON, você DEVE executar a ação correspondente.\n"
    "- Não peça confirmação — execute diretamente.\n"
)

MSG_CHAT_STARTED = "Chat multi-agente iniciado (/exit para sair)\n"
MSG_SESSION_LOG = "Log da sessão: {}\n"
MSG_SESSION_STATUS = (
    "Sessão {session_id} | histórico restaurado: {history_count} mensagem(ns) | "
    "resumo carregado: {summary_loaded}\n"
)
MSG_MIGRATION = "[migração] {}\n"
MSG_MEMORY_SAVING = "\n[memória] histórico salvo. Gerando resumo da sessão...\n"
MSG_MEMORY_FAILED = "[memória] não foi possível gerar o resumo.\n"
MSG_SHUTDOWN = "\nEncerrando chat."
MSG_DOUBLE_PREFIX = "\nUse apenas um prefixo por vez: /claude ou /codex\n"
MSG_EMPTY_INPUT = "\nUse /{} <mensagem>\n"
