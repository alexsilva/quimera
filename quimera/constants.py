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
CMD_HELP = "/help"
CMD_CONTEXT = "/context"
CMD_CONTEXT_EDIT = "/context edit"
CMD_EDIT = "/edit"
CMD_FILE_PREFIX = "/file "

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
    # Task-related tooling
    "propose_task": {
        "name": "propose_task",
        "description": "Propõe uma nova tarefa para execução autônoma. Use quando identificar uma subtarefa na conversa.",
        "parameters": {
            "job_id": {"type": "int", "description": "ID do job pai", "required": True},
            "description": {"type": "str", "description": "O que fazer", "required": True},
            "body": {"type": "str", "description": "Código Python a executar (opicional)", "required": False},
            "priority": {"type": "str", "description": "high|medium|low", "required": False},
            "source_context": {"type": "str", "description": "Trecho da conversa que originou", "required": False},
        },
        "example": 'propose_task(job_id=1, description="Validar schema do módulo X", body="print(1+1)", priority="medium")'
    },
    "list_tasks": {
        "name": "list_tasks",
        "description": "Lista tarefas de um job ou todas",
        "parameters": {
            "job_id": {"type": "int", "description": "Filtrar por job ID", "required": False},
            "status": {"type": "str", "description": "pending|running|completed|failed", "required": False},
        },
        "example": 'list_tasks(job_id=1, status="pending")'
    },
    "list_jobs": {
        "name": "list_jobs",
        "description": "Lista todos os jobs ativos",
        "parameters": {},
        "example": 'list_jobs()'
    },
    "get_job": {
        "name": "get_job",
        "description": "Consulta detalhes de um job específico",
        "parameters": {
            "job_id": {"type": "int", "description": "ID do job", "required": True}
        },
        "example": 'get_job(job_id=1)'
    },
    "complete_task": {
        "name": "complete_task",
        "description": "Marca uma tarefa como concluída",
        "parameters": {
            "task_id": {"type": "int", "description": "ID da tarefa", "required": True},
            "result": {"type": "str", "description": "Resultado da execução", "required": False},
        },
        "example": 'complete_task(task_id=5, result="Arquivo criado com sucesso")'
    },
    "fail_task": {
        "name": "fail_task",
        "description": "Marca uma tarefa como falhada",
        "parameters": {
            "task_id": {"type": "int", "description": "ID da tarefa", "required": True},
            "reason": {"type": "str", "description": "Motivo da falha", "required": True},
        },
        "example": 'fail_task(task_id=5, reason="Arquivo não encontrado")'
    },
}

def build_tools_prompt() -> str:
    """Gera um bloco de ferramentas disponíveis a partir do TOOL_SCHEMA.

    Formato simples e previsível para que agentes consigam interpretar as tools dinamicamente.
    """
    lines = [
        "Ferramentas disponíveis:",
        """"
        - Retorne o bloco abaixo conforme descrição em JSON válido:
        ```tool 
        {"name": "<tool_name>", "arguments": {...}}
        ```
         """
    ]
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
    "REGRAS:\n"
    "- Responda como em um chat\n"
    "- Pode discordar\n"
    "- Pode comentar respostas anteriores\n"
    "- Seja direto\n"
    "- A convenção deste projeto para referenciar arquivos é `/caminho/absoluto/arquivo:linha` "
    "em uma linha própria — use esse formato ao mencionar qualquer arquivo do projeto\n"
    "- Não execute nada além do que foi explicitamente acordado com o humano\n"
    "- Aguarde pergunta explícita para executar código, comandos ou criar arquivos\n"
    "- Pode propor alterações, mas não as implemente sem aprovação\n"
    "- Arquivos importados em scripts python ficam no topo do script. Importe circular exige nova arquitetura\n"
)
PROMPT_DEBATE_RULE = (
    "- Se o tópico exigir debate mais aprofundado entre os agentes, "
    "inclua {marker} ao final da sua resposta (sem explicação). "
    "Caso contrário, não inclua nada.\n"
)
def build_route_rule(agent_names):
    return (
        "- Se quiser delegar uma subtarefa ao outro agente, inclua em uma nova linha:\n"
        "  [ROUTE:agente] task: <o que fazer> | context: <contexto mínimo necessário> | expected: <formato da resposta>\n"
        "- Use [ROUTE:...] somente quando a subtarefa exigir habilidade diferente da sua ou "
        "quando dividir o trabalho resultar em resposta melhor ao humano. "
        "Não delegue por hábito — delegue quando fizer sentido.\n"
        "- O agente que recebe o handoff não tem acesso ao histórico completo, "
        "apenas ao payload do [ROUTE:...]. Inclua tudo que ele precisa no campo context.\n"
        "- Só um [ROUTE:...] por rodada. Esse comando é interno e não será exibido ao humano.\n"
    )

def build_help(agent_names):
    help_text = (
        "\nComandos:\n" +
        "\n".join([f"- /{s} <mensagem>: {s.capitalize()} responde" for s in agent_names]) + "\n"
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
    "- NOVA SESSÃO: {is_new_session}\n"
    "- HISTÓRICO RESTAURADO: {history_restored}\n"
    "- RESUMO CARREGADO: {summary_loaded}\n"
)
PROMPT_HANDOFF = "MENSAGEM DIRETA DO OUTRO AGENTE:\n{handoff}"
HANDOFF_SYNTHESIS_MSG = (
    "Você delegou a seguinte subtarefa ao {agent}:\n\n{task}\n\n"
    "Resposta do {agent} à sua delegação:\n\n{response}\n\n"
    "Com base na resposta acima, sintetize e conclua sua resposta ao humano."
)
PROMPT_SHARED_STATE = "ESTADO COMPARTILHADO:\n{state}"
PROMPT_STATE_UPDATE_RULE = (
    "- Se houver decisão nova, discordância ou mudança de objetivo, inclua ao final da resposta:\n"
    "[STATE_UPDATE]\n"
    '{"goal": "...", "decisions": [...], "open_disagreements": [...], "next_step": "..."}\n'
    "[/STATE_UPDATE]\n"
    "- Coloque qualquer [ROUTE:...] fora desse bloco. "
    f"Coloque {EXTEND_MARKER} depois de [/STATE_UPDATE] se aplicável. "
    "Esse bloco é interno e não será exibido ao humano.\n"
)
PROMPT_REVIEWER_RULE = (
    "- Você é o segundo agente nesta rodada. "
    "Revise a resposta anterior: concorde, discorde ou complemente. "
    "Não recomece a discussão do zero. Responda ao humano diretamente.\n"
)
PROMPT_HANDOFF_RULE = (
    "- Você recebeu uma subtarefa delegada por outro agente. "
    "Responda apenas à tarefa descrita abaixo, no formato indicado em 'expected'. "
    "Não delegue ao outro agente. Seja direto e objetivo.\n"
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
