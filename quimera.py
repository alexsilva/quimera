import os
import subprocess
from datetime import datetime
from pathlib import Path

history = []
BASE_DIR = Path(__file__).parent
CONTEXT_FILE = BASE_DIR / "quimera_context.md"
LOGS_DIR = BASE_DIR / "logs"


def get_log_file():
    LOGS_DIR.mkdir(exist_ok=True)
    return LOGS_DIR / f"sessao-{datetime.now().strftime('%Y-%m-%d')}.txt"


def append_log(role, content):
    log_file = get_log_file()
    timestamp = datetime.now().strftime("%H:%M:%S")
    message = f"[{timestamp}] [{role.upper()}] {content}\n"
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(message)


def run(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        print(f"[erro] comando não encontrado: {cmd[0]} ({exc})")
        return None

    output = result.stdout.strip()
    error = result.stderr.strip()

    if result.returncode != 0:
        print(f"[erro] {' '.join(cmd)} retornou código {result.returncode}")
        if error:
            print(error)
        return None

    if not output:
        if error:
            print(f"[erro] {' '.join(cmd)} não retornou saída válida")
            print(error)
        return None

    return output


def call_claude(history):
    prompt = build_prompt("claude", history)
    return run(["claude", "-p", prompt])


def call_codex(history):
    prompt = build_prompt("codex", history)
    return run(["codex", "exec", "--skip-git-repo-check", prompt])


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


def handle_command(user_input):
    command = user_input.strip()

    if command == "/context":
        show_context()
        return True

    if command == "/context edit":
        edit_context()
        return True

    return False


def build_prompt(agent, history):
    context = load_context()
    base = f"""
Você é {agent.upper()} em uma conversa com:
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
        base += f"""

CONTEXTO PERSISTENTE:
{context}
"""

    base += """

CONVERSA:
"""

    for msg in history[-10:]:
        base += f"\n[{msg['role'].upper()}]: {msg['content']}"

    base += f"\n[{agent.upper()}]:"
    return base


def main():
    print("💬 Chat multi-agente iniciado (/exit para sair)\n")
    print(f"Log da sessao: {get_log_file()}\n")

    try:
        while True:
            # 👤 HUMANO
            user = input("Você: ")

            if user == "/exit":
                break

            if handle_command(user):
                continue

            history.append({"role": "human", "content": user})
            append_log("human", user)

            # 🤖 CLAUDE responde
            claude = call_claude(history)
            if claude is not None:
                print(f"\nClaude: {claude}\n")
                history.append({"role": "claude", "content": claude})
                append_log("claude", claude)
            else:
                print("\nClaude: [sem resposta válida]\n")

            # 🤖 CODEX responde
            codex = call_codex(history)
            if codex is not None:
                print(f"Codex: {codex}\n")
                history.append({"role": "codex", "content": codex})
                append_log("codex", codex)
            else:
                print("Codex: [sem resposta válida]\n")
    except KeyboardInterrupt:
        print("\nEncerrando chat.")


if __name__ == "__main__":
    main()
