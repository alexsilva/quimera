EXTEND_MARKER = "[DEBATE]"
ROUTE_PREFIX = "[ROUTE:"

CMD_EXIT = "/exit"
CMD_CONTEXT = "/context"
CMD_CONTEXT_EDIT = "/context edit"

PREFIX_CLAUDE = "/claude"
PREFIX_CODEX = "/codex"

AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"
DEFAULT_FIRST_AGENT = AGENT_CLAUDE
AGENT_SEQUENCE = ((PREFIX_CODEX, AGENT_CODEX), (PREFIX_CLAUDE, AGENT_CLAUDE))

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
)
PROMPT_DEBATE_RULE = (
    "- Se o tópico exigir debate mais aprofundado entre os agentes, "
    "inclua {marker} ao final da sua resposta (sem explicação). "
    "Caso contrário, não inclua nada.\n"
)
PROMPT_ROUTE_RULE = (
    "- Se quiser pedir algo diretamente ao outro agente, inclua em uma nova linha "
    "[ROUTE:claude] <mensagem> ou [ROUTE:codex] <mensagem>. "
    "Esse comando é interno e não será exibido ao humano.\n"
)
PROMPT_PARTICIPANTS = "- HUMANO\n- CLAUDE\n- CODEX\n"
PROMPT_HANDOFF = "MENSAGEM DIRETA DO OUTRO AGENTE:\n{handoff}"

MSG_CHAT_STARTED = "Chat multi-agente iniciado (/exit para sair)\n"
MSG_SESSION_LOG = "Log da sessão: {}\n"
MSG_MIGRATION = "[migração] {}\n"
MSG_MEMORY_SAVING = "\n[memória] histórico salvo. Gerando resumo da sessão...\n"
MSG_MEMORY_FAILED = "[memória] não foi possível gerar o resumo.\n"
MSG_SHUTDOWN = "\nEncerrando chat."
MSG_DOUBLE_PREFIX = "\nUse apenas um prefixo por vez: /claude ou /codex\n"
MSG_EMPTY_INPUT = "\nUse /{} <mensagem>\n"
