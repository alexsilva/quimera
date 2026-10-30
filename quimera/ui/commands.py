"""Comandos, mensagens e helpers de UI do Quimera."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


DEFAULT_FIRST_AGENT = "claude"
INPUT_PROMPT = "Você: "

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
CMD_DEBATE = "/debate"
CMD_WORKFLOW = "/workflow"
CMD_BUGS = "/bugs"
CMD_STATS = "/stats"
CMD_STOP = "/stop"
CMD_RESET = "/reset"
CMD_APPROVE = "/approve"
CMD_APPROVE_ALL = "/approve-all"
CMD_POLICY = "/policy"
CMD_SANDBOX = "/sandbox"
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

# Prefixos de roteamento do chat. `s/` congela a rotação num agente, `o/`
# ativa o orquestrador e `r/` desfaz os dois. São controle, não mensagem:
# o que chega ao agente (e ao feed) é só o texto depois do alvo.
ROUTING_PREFIX_FREEZE = "s/"
ROUTING_PREFIX_ORCHESTRATOR = "o/"
ROUTING_PREFIX_ROTATION = "r/"


@dataclass(frozen=True)
class RoutingCommand:
    """`s/<agente> [mensagem]`, `o/<agente> [mensagem]` ou `r/` já separados."""

    prefix: str
    agent: str = ""
    message: str = ""


def parse_routing_command(text: str) -> RoutingCommand | None:
    """Separa prefixo, agente e mensagem de um comando de roteamento.

    Devolve ``None`` quando o texto não é comando de roteamento: ``s/`` e
    ``o/`` sem alvo seguem como mensagem comum. ``r/`` é só controle, então
    qualquer texto depois dele é descartado, como no despacho.
    """
    command = str(text or "").strip()
    if command.startswith(ROUTING_PREFIX_ROTATION):
        return RoutingCommand(ROUTING_PREFIX_ROTATION)
    for prefix in (ROUTING_PREFIX_FREEZE, ROUTING_PREFIX_ORCHESTRATOR):
        if not command.startswith(prefix):
            continue
        parts = command[len(prefix):].strip().split(None, 1)
        if not parts:
            return None
        message = parts[1].strip() if len(parts) > 1 else ""
        return RoutingCommand(prefix, parts[0], message)
    return None

# Messages
MSG_CHAT_STARTED = "Chat multi-agente iniciado (/exit para sair)\n"
MSG_SESSION_LOG = "Log da sessão:\n  {}\n"
MSG_SESSION_STATUS = "Sessão {session_id} | resumo carregado: {summary_loaded}\n"
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
            "- /debate <tema>: coordena agentes em rodadas até um veredito ou workflow auditável\n"
            "- /debate status|cancel|list|show <id>|apply <id>: controla debates\n"
            "- /workflow [list|show <id>|pause <id>|resume <id>|cancel <id>]: acompanha e controla workflows distribuídos\n"
            "- /bugs [list|show|close|analyze|stats]: operações de diagnóstico com bugs detectados automaticamente\n"
            "- /stats [<agente>|json [<agente>]|reset [<agente>]]: consulta ou zera as métricas de entrega dos agentes\n"
            "- /stop [#número|agente]: para somente uma execução de chat; sem alvo lista quando há várias\n"
            "- /planning <mensagem>: modo planejamento — workspace somente leitura, sem edição de arquivos\n"
            "- /analysis <mensagem>: modo análise — somente leitura, sem edição de arquivos\n"
            "- /design <mensagem>: modo design — arquitetura e design sem execução\n"
            "- /review <mensagem>: modo revisão — somente revisão de código, sem edições\n"
            "- /execute <mensagem>: modo execução — acesso completo a ferramentas e remove restrições do modo anterior\n"
            "- /agents: lista os agentes disponíveis\n"
            "- /connect <agente>: configura interativamente a conexão de um agente e persiste no base_dir\n"
            "- /disconnect <agente>: remove a conexão persistida de um agente\n"
            "- /clear: limpa o chat e mantém o cabeçalho da sessão (logo, projeto, MCP, logs)\n"
            "- /config: abre a janela popup de configurações\n"
            "- /prompt [agente]: simula o prompt final e mostra análise dos blocos\n"
            "- /context [show]: mostra o contexto atual\n"
            "- /context edit: abre o contexto persistente no editor ($EDITOR, ou nano/vim/vi como fallback)\n"
            "- /context branch [branch]: mostra ou define a branch de template de contexto persistente\n"
            "- /context resumer [<agente>|clear]: mostra, define ou remove o agente preferido para resumir o contexto"
            " (fallback automático para outro agente da sessão se ausente ou falhar)\n"
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
            "- /sandbox [status|on|off]: controla o confinamento de comandos ao workspace\n"
            "- /help: mostra esta ajuda\n"
            "- /exit: encerra a sessão\n"
    )
    return help_text


def build_agents_help(agent_names: Sequence[str], unavailable: Mapping[str, str] | None = None) -> str:
    """Monta a lista de agentes do pool; ``unavailable`` anota os suspensos (nome -> motivo)."""
    notes = unavailable or {}
    lines = []
    for name in agent_names:
        line = f"- /{name} <mensagem>: {name.capitalize()} responde"
        if name in notes:
            line += f" — indisponível: {notes[name]}"
        lines.append(line)
    return "\nAgentes:\n" + ("\n".join(lines) if lines else "- nenhum")


def format_cooldown(seconds: float) -> str:
    """Tempo restante de uma suspensão em texto curto (``45 s``, ``12 min``, ``1 h``)."""
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total} s"
    if total < 3600:
        return f"{total // 60} min"
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    return f"{hours} h" if minutes == 0 else f"{hours} h {minutes} min"
