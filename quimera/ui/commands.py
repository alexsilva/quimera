"""Comandos, mensagens e helpers de UI do Quimera."""
from __future__ import annotations

from typing import Sequence


DEFAULT_FIRST_AGENT = "claude"
INPUT_PROMPT = "Você: "

# Protocol markers
EXTEND_MARKER = "[DEBATE]"

# Commands
CMD_EXIT = "/exit"
CMD_CLEAR = "/clear"
CMD_PROMPT = "/prompt"
CMD_HELP = "/help"
CMD_AGENTS = "/agents"
CMD_CONNECT = "/connect"
CMD_DISCONNECT = "/disconnect"
CMD_RELOAD = "/reload"
CMD_CONTEXT = "/context"
CMD_CONTEXT_EDIT = "/context-edit"
CMD_CONTEXT_BRANCH = "/context-branch"
CMD_EDIT = "/edit"
CMD_FILE_PREFIX = "/file"
CMD_TASK = "/task"
CMD_BUGS = "/bugs"
CMD_RESET = "/reset"
CMD_APPROVE = "/approve"
CMD_APPROVE_ALL = "/approve-all"
CMD_POLICY = "/policy"
CMD_CONFIG = "/config"
CMD_ALIASES = {
    "/e": CMD_EDIT,
    "/r": CMD_CONTEXT,
    "/g": CMD_HELP,
    "/y": CMD_APPROVE,
    "/a": CMD_APPROVE,
    "/aa": CMD_APPROVE_ALL,
}
USER_ROLE = "human"

# Messages
MSG_CHAT_STARTED = "Chat multi-agente iniciado (/exit para sair)\n"
MSG_SESSION_LOG = "Log da sessão:\n  {}\n"
MSG_SESSION_STATUS = "Sessão {session_id} | resumo carregado: {summary_loaded}\n"
MSG_MIGRATION = "[migração] {}\n"
MSG_MEMORY_SAVING = "[memória] histórico salvo. Gerando resumo da sessão..."
MSG_MEMORY_FAILED = "[memória] não foi possível gerar o resumo."
MSG_SHUTDOWN = "Encerrando chat."
MSG_DOUBLE_PREFIX = "\nUse apenas um prefixo por vez: /claude ou /codex\n"
MSG_EMPTY_INPUT = "\nUse /{} <mensagem>\n"


def build_help(agent_names: Sequence[str]) -> str:
    """Monta help."""
    help_text = (
            "\nComandos:\n" +
            "- /task <descrição>: cria uma task explícita do humano e roteia para o melhor agente\n"
            "- /bugs [list|show|close|analyze|stats]: operações de diagnóstico com bugs detectados automaticamente\n"
            "- /planning <mensagem>: modo planejamento — workspace somente leitura, sem edição de arquivos\n"
            "- /analysis <mensagem>: modo análise — somente leitura, sem edição de arquivos\n"
            "- /design <mensagem>: modo design — arquitetura e design sem execução\n"
            "- /review <mensagem>: modo revisão — somente revisão de código, sem edições\n"
            "- /execute <mensagem>: modo execução — acesso completo a ferramentas e remove restrições do modo anterior\n"
            "- /agents: lista os agentes disponíveis\n"
            "- /connect <agente>: configura interativamente a conexão de um agente e persiste no base_dir\n"
            "- /disconnect <agente>: remove a conexão persistida de um agente\n"
            "- /clear: limpa a tela do terminal\n"
            "- /config: abre a janela popup de configurações\n"
            "- /prompt [agente]: simula o prompt final e mostra análise dos blocos\n"
            "- /context [show]: mostra o contexto atual\n"
            "- /context edit: abre o contexto persistente no editor ($EDITOR, ou nano/vim/vi como fallback)\n"
            "- /context branch [branch]: mostra ou define a branch de template de contexto persistente\n"
            "- /edit: abre o editor ($EDITOR, ou nano/vim/vi como fallback) para compor uma mensagem longa\n"
            "- /file <caminho>: usa o conteúdo de um arquivo como mensagem\n"
            "- /reset state: limpa o shared_state (objetivo, passo, critérios)\n"
            "- /reset history: limpa o histórico da conversa\n"
            "- /reset all: limpa shared_state e histórico\n"
            "- s/<agente> [mensagem]: congela o agente primário para este agente\n"
            "- o/<agente> [mensagem]: ativa modo orquestrador — agente analisa o pedido e delega aos demais\n"
            "- r/: desativa congelamento ou modo orquestrador, volta a rotacionar\n"
            "- /approve: pré-aprova a próxima chamada de ferramenta\n"
            "- /approve-all: aprova automaticamente todas as chamadas de ferramenta seguintes\n"
            "- /help: mostra esta ajuda\n"
            "- /exit: encerra a sessão\n"
    )
    return help_text


def build_agents_help(agent_names: Sequence[str]) -> str:
    """Monta a lista de agentes disponíveis."""
    agents = "\n".join(f"- /{name} <mensagem>: {name.capitalize()} responde" for name in agent_names)
    return "\nAgentes:\n" + (agents if agents else "- nenhum")
