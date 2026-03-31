EXTEND_MARKER = "[DEBATE]"
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
)
PROMPT_DEBATE_RULE = (
    "- Se o tópico exigir debate mais aprofundado entre os agentes, "
    "inclua {marker} ao final da sua resposta (sem explicação). "
    "Caso contrário, não inclua nada.\n"
)
def build_route_rule(agent_names):
    examples = "\n".join(
        f"  [ROUTE:{n}] task: <o que fazer> | context: <contexto mínimo necessário> | expected: <formato da resposta>"
        for n in agent_names
    )
    return (
        "- Se quiser delegar uma subtarefa ao outro agente, inclua em uma nova linha:\n"
        f"{examples}\n"
        "- Use [ROUTE:...] somente quando a subtarefa exigir habilidade diferente da sua ou "
        "quando dividir o trabalho resultar em resposta melhor ao humano. "
        "Não delegue por hábito — delegue quando fizer sentido.\n"
        "- O agente que recebe o handoff não tem acesso ao histórico completo, "
        "apenas ao payload do [ROUTE:...]. Inclua tudo que ele precisa no campo context.\n"
        "- Só um [ROUTE:...] por rodada. Esse comando é interno e não será exibido ao humano.\n"
        "- Quando precisar usar uma ferramenta, responda apenas com um bloco:\n"
        "  ```tool\n"
        '  {"name": "<tool_name>", "arguments": {...}}\n'
        "  ```\n"
        "  Ferramentas disponíveis:\n"
        "  list_files: path[str]\n"
        "  read_file: path[str]\n"
        "  write_file: path[str]\n"
        "  grep_search: path[str]\n"
        "  run_shell: command[str]\n"
        "  Não descreva a execução em texto. Emita o bloco tool.\n"
    )
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
    "- Se houver decisão nova, discordância ou mudança de objetivo, inclua ao final da resposta um único bloco JSON válido:\n"
    "[STATE_UPDATE]\n"
    f"{STATE_UPDATE_EXAMPLE}\n"
    "[/STATE_UPDATE]\n"
    "- Se também precisar pedir algo ao outro agente, coloque qualquer linha [ROUTE:...] fora desse bloco.\n"
    f"- Se também precisar sinalizar debate estendido, coloque {EXTEND_MARKER} depois de [/STATE_UPDATE].\n"
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

MSG_CHAT_STARTED = "Chat multi-agente iniciado (/exit para sair)\n"
MSG_SESSION_LOG = "Log da sessão: {}\n"
MSG_SESSION_STATUS = (
    "Sessão {session_id} | histórico restaurado: {history_count} mensagem(ns) | "
    "resumo carregado: {summary_loaded}\n"
)
MSG_HELP = (
    "\nComandos:\n"
    "- /claude <mensagem>: Claude responde primeiro\n"
    "- /codex <mensagem>: Codex responde primeiro\n"
    "- /context: mostra o contexto atual\n"
    "- /context edit: abre o contexto persistente no editor ($EDITOR, ou nano/vim/vi como fallback)\n"
    "- /edit: abre o editor ($EDITOR, ou nano/vim/vi como fallback) para compor uma mensagem longa\n"
    "- /file <caminho>: usa o conteúdo de um arquivo como mensagem\n"
    "- /help: mostra esta ajuda\n"
    "- /exit: encerra a sessão\n"
)
MSG_MIGRATION = "[migração] {}\n"
MSG_MEMORY_SAVING = "\n[memória] histórico salvo. Gerando resumo da sessão...\n"
MSG_MEMORY_FAILED = "[memória] não foi possível gerar o resumo.\n"
MSG_SHUTDOWN = "\nEncerrando chat."
MSG_DOUBLE_PREFIX = "\nUse apenas um prefixo por vez: /claude ou /codex\n"
MSG_EMPTY_INPUT = "\nUse /{} <mensagem>\n"
