import os
import json
import subprocess
from datetime import datetime
from pathlib import Path

history = []
BASE_DIR = Path(__file__).parent
CONTEXT_FILE = BASE_DIR / "quimera_context.md"
LOGS_DIR = BASE_DIR / "logs"
SUMMARY_MARKER = "## Resumo da ultima sessao"
SESSION_STAMP = datetime.now().strftime("%Y-%m-%d-%H%M%S")


def get_log_file():
    LOGS_DIR.mkdir(exist_ok=True)
    return LOGS_DIR / f"sessao-{datetime.now().strftime('%Y-%m-%d')}.txt"


def get_history_file():
    LOGS_DIR.mkdir(exist_ok=True)
    return LOGS_DIR / f"sessao-{SESSION_STAMP}.json"


def append_log(role, content):
    log_file = get_log_file()
    timestamp = datetime.now().strftime("%H:%M:%S")
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [{role.upper()}] {content}\n")


def save_history(hist):
    payload = {
        "session_id": SESSION_STAMP,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "messages": hist,
    }
    with get_history_file().open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_last_history():
    LOGS_DIR.mkdir(exist_ok=True)
    json_files = sorted(LOGS_DIR.glob("sessao-*.json"), reverse=True)
    if not json_files:
        return []

    latest = json_files[0]
    try:
        with latest.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            messages = data.get("messages", [])
        else:
            messages = []

        if messages:
            print(f"[memoria] historico restaurado de {latest.name} ({len(messages)} mensagens)\n")
        return messages
    except (json.JSONDecodeError, OSError):
        return []


def run(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        print(f"[erro] comando nao encontrado: {cmd[0]} ({exc})")
        return None

    output = result.stdout.strip()
    error = result.stderr.strip()

    if result.returncode != 0:
        print(f"[erro] {' '.join(cmd)} retornou codigo {result.returncode}")
        if error:
            print(error)
        return None

    if not output:
        if error:
            print(f"[erro] {' '.join(cmd)} nao retornou saida valida")
            print(error)
        return None

    return output


def call_claude(hist):
    return run(["claude", "-p", build_prompt("claude", hist)])


def call_codex(hist):
    return run(["codex", "exec", "--skip-git-repo-check", build_prompt("codex", hist)])


def load_context():
    if not CONTEXT_FILE.exists():
        return ""
    return CONTEXT_FILE.read_text(encoding="utf-8").strip()


def show_context():
    context = load_context()
    if not context:
        print("\n[contexto vazio]\n")
        return
    print(f"\n{context}\n")


def edit_context():
    editor = os.environ.get("EDITOR")
    if not editor:
        print("\nDefina a variavel EDITOR para usar /context edit.\n")
        return
    try:
        subprocess.run([editor, str(CONTEXT_FILE)], check=True)
    except FileNotFoundError:
        print(f"\nEditor nao encontrado: {editor}\n")
    except subprocess.CalledProcessError as exc:
        print(f"\nFalha ao abrir o contexto no editor (codigo {exc.returncode}).\n")


def summarize_session(hist):
    if not hist:
        return None

    conversation = "\n".join(
        f"[{m['role'].upper()}]: {m['content']}" for m in hist
    )
    prompt = f"""Voce e um assistente de memoria. Analise a conversa abaixo e gere um resumo estruturado em markdown.

O resumo deve conter:
- O que foi discutido (topicos principais)
- Decisoes tomadas (se houver)
- Pendencias ou proximos passos (se houver)

Seja conciso. Maximo 20 linhas. Nao use emojis. Escreva em portugues.

CONVERSA:
{conversation}

RESUMO:"""

    return run(["claude", "-p", prompt])


def update_context_with_summary(summary):
    context = load_context()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_section = f"{SUMMARY_MARKER}\n\n_Gerado em {timestamp}_\n\n{summary}"

    if SUMMARY_MARKER in context:
        before = context.split(SUMMARY_MARKER)[0].rstrip()
        updated = f"{before}\n\n{new_section}"
    else:
        updated = f"{context}\n\n{new_section}"

    CONTEXT_FILE.write_text(updated.strip() + "\n", encoding="utf-8")
    print(f"[memoria] resumo salvo em {CONTEXT_FILE.name}\n")


def handle_command(user_input):
    command = user_input.strip()

    if command == "/context":
        show_context()
        return True

    if command == "/context edit":
        edit_context()
        return True

    return False


def build_prompt(agent, hist):
    context = load_context()
    base = f"""Voce e {agent.upper()} em uma conversa com:
- HUMANO
- CLAUDE
- CODEX

REGRAS:
- Responda como em um chat
- Pode discordar
- Pode comentar respostas anteriores
- Seja direto
"""

    if context:
        base += f"\n\nCONTEXTO PERSISTENTE:\n{context}"

    base += "\n\nCONVERSA:"

    for msg in hist[-20:]:
        base += f"\n[{msg['role'].upper()}]: {msg['content']}"

    base += f"\n[{agent.upper()}]:"
    return base


def parse_routing(user_input):
    stripped = user_input.lstrip()
    lowered = stripped.lower()

    for prefix, agent in [("/codex", "codex"), ("/claude", "claude")]:
        if lowered == prefix:
            return agent, ""
        if lowered.startswith(f"{prefix} "):
            return agent, stripped[len(prefix):].lstrip()

    return "claude", user_input


def call_agent(agent, hist):
    if agent == "claude":
        return call_claude(hist)
    return call_codex(hist)


def print_response(agent, response):
    label = agent.capitalize()
    if response is not None:
        print(f"\n{label}: {response}\n")
    else:
        print(f"\n{label}: [sem resposta valida]\n")


def shutdown(hist):
    if not hist:
        return

    save_history(hist)
    print("\n[memoria] historico salvo. Gerando resumo da sessao...\n")

    summary = summarize_session(hist)
    if summary:
        update_context_with_summary(summary)
    else:
        print("[memoria] nao foi possivel gerar o resumo.\n")


def main():
    global history
    history = load_last_history()

    print("Chat multi-agente iniciado (/exit para sair)\n")
    print(f"Log da sessao: {get_log_file()}\n")

    try:
        while True:
            user = input("Voce: ")

            if user == "/exit":
                break

            if handle_command(user):
                continue

            first_agent, message = parse_routing(user)

            if not message.strip():
                print(f"\nUse /{first_agent} <mensagem>\n")
                continue

            second_agent = "codex" if first_agent == "claude" else "claude"

            history.append({"role": "human", "content": message})
            append_log("human", message)
            save_history(history)

            for agent in (first_agent, second_agent):
                response = call_agent(agent, history)
                print_response(agent, response)
                if response is not None:
                    history.append({"role": agent, "content": response})
                    append_log(agent, response)
                    save_history(history)

    except KeyboardInterrupt:
        print("\nEncerrando chat.")
    finally:
        shutdown(history)


if __name__ == "__main__":
    main()
