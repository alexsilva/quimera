import os
import queue
import select
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path


def read_user_input(app, prompt, timeout: int, input_fn=input) -> str | None:
    if timeout and timeout > 0:
        value = app._read_user_input_with_timeout(prompt, timeout)
        if value is None:
            app.renderer.show_system(f"*idle* ({timeout}s sem activity)")
            return None
        return value

    if timeout == 0:
        try:
            stdin = app._stdin()
            if stdin is None:
                return None
            if stdin.isatty():
                return app._read_user_input_nonblocking_tty(prompt)
            if select.select([stdin], [], [], 0)[0]:
                line = stdin.readline()
                if line == "":
                    return None
                return line.rstrip("\r\n")
            time.sleep(0.01)
            return None
        except Exception:
            return None

    try:
        app._nonblocking_prompt_visible = False
        return input_fn(prompt)
    except EOFError:
        if timeout == 0:
            return None
        raise
    except KeyboardInterrupt:
        app._nonblocking_prompt_visible = False
        print()
        raise


def read_user_input_nonblocking_tty(app, prompt: str) -> str | None:
    if app._nonblocking_input_queue is None:
        app._nonblocking_input_queue = queue.Queue()

    try:
        status, value = app._nonblocking_input_queue.get_nowait()
    except queue.Empty:
        thread = app._nonblocking_input_thread
        if thread is None or not thread.is_alive():
            app._start_nonblocking_input_reader(prompt)
        return None

    app._nonblocking_input_status = "idle"
    app._nonblocking_input_thread = None
    app._nonblocking_prompt_text = ""
    if status == "line":
        return value
    return None


def start_nonblocking_input_reader(app, prompt: str, input_fn=input) -> None:
    if app._nonblocking_input_queue is None:
        app._nonblocking_input_queue = queue.Queue()

    app._nonblocking_input_status = "reading"
    app._nonblocking_prompt_text = prompt

    def _reader() -> None:
        try:
            value = input_fn(prompt)
        except EOFError:
            app._nonblocking_input_queue.put(("eof", None))
        except KeyboardInterrupt:
            app._nonblocking_input_queue.put(("interrupt", None))
        except Exception:
            app._nonblocking_input_queue.put(("error", None))
        else:
            app._nonblocking_input_queue.put(("line", value))

    app._nonblocking_input_thread = threading.Thread(target=_reader, daemon=True)
    app._nonblocking_input_thread.start()


def read_user_input_with_timeout(prompt: str, timeout: int, input_fn=input):
    q = queue.Queue()

    def _reader():
        try:
            q.put(input_fn(prompt))
        except Exception:
            q.put(None)

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


def read_from_editor(app):
    editor_env = os.environ.get("EDITOR", "")
    if editor_env:
        editor_parts = shlex.split(editor_env)
    else:
        fallbacks = ["nano", "vim", "vi"]
        editor_parts = next(([editor] for editor in fallbacks if shutil.which(editor)), None)
        if not editor_parts:
            app.renderer.show_error("\nNenhum editor encontrado. Defina $EDITOR ou instale nano/vim.\n")
            return None

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        subprocess.run([*editor_parts, tmp_path], check=True)
        content = Path(tmp_path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        app.renderer.show_error(f"\nEditor não encontrado: {editor_parts[0]}\n")
        return None
    except subprocess.CalledProcessError as exc:
        app.renderer.show_error(f"\nEditor encerrou com erro (código {exc.returncode}).\n")
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return content or None


def read_from_file(app, path_str):
    path = Path(path_str).expanduser()
    if not path.exists():
        app.renderer.show_error(f"\nArquivo não encontrado: {path}\n")
        return None
    content = path.read_text(encoding="utf-8").strip()
    return content or None
