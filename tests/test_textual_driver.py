"""``QuimeraLinuxDriver`` com o ``LinuxDriver`` real num pseudo-terminal.

O lado "terminal" do PTY é emulado aqui: responde a cada ``CSI 6 n`` com a
coluna que um terminal com a tabela de larguras dada teria alcançado.
"""
from __future__ import annotations

import codecs
import fcntl
import json
import os
import re
import struct
import subprocess
import sys
import termios
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from rich._unicode_data import VERSIONS as RICH_VERSIONS

from quimera.ui.textual.app import run_textual_quimera_app
from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.cell_widths import PROBES, VS16
from quimera.ui.textual.driver import QuimeraLinuxDriver
from tests.test_textual_cell_widths import table_cells, xtermjs_unicode6_cells

LATEST = RICH_VERSIONS[-1]
PROBE_RE = re.compile(r"\x1b\[1;1H(.*?)\x1b\[6n", re.DOTALL)
PROBES_DONE = "\x1b[1;1H\x1b[2K"  # o driver limpa a linha da sondagem ao terminar

_CHILD_APP = """
import json, os, sys
from rich.cells import cell_len
from textual.app import App
from textual.widgets import Static
from quimera.ui.textual.driver import QuimeraLinuxDriver

QuimeraLinuxDriver.probe_timeout = float(sys.argv[2])


class Probe(App):
    def __init__(self):
        super().__init__(driver_class=QuimeraLinuxDriver)

    def compose(self):
        yield Static("\\u2601\\ufe0f nuvem \\U0001fa90 planeta")

    def on_mount(self):
        self.call_after_refresh(self._finish)

    def _finish(self):
        with self.suspend():  # resume_application_mode() nao deve sondar de novo
            pass
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            json.dump({
                "env": os.environ.get("UNICODE_VERSION"),
                "materialize_vs16": self._driver._materialize_vs16,
                "planet_cells": cell_len("\\U0001fa90"),
                "robot_cells": cell_len("\\U0001f916"),
            }, fh)
        self.exit()


Probe().run()
"""


def _run_app_in_pty(cells, tmp_path: Path, probe_timeout: float = 5.0) -> tuple[dict, str]:
    """Roda a app filha num PTY cujo terminal responde às sondas com ``cells`` (``None``: nunca)."""
    try:
        master, slave = os.openpty()
    except OSError as error:  # pragma: no cover - ambiente sem PTY
        pytest.skip(f"sem pseudo-terminal: {error}")
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    result_path = tmp_path / "calibration.json"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"UNICODE_VERSION", "COLUMNS", "LINES"}
    }
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD_APP, str(result_path), str(probe_timeout)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        start_new_session=True,
    )
    os.close(slave)
    captured: list[str] = []

    def terminal() -> None:
        decode = codecs.getincrementaldecoder("utf-8")().decode
        buffer = ""
        answered = 0
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            buffer += decode(chunk)
            if cells is not None:
                for probe in PROBE_RE.findall(buffer)[answered:]:
                    os.write(master, f"\x1b[1;{1 + cells(probe)}R".encode())
                    answered += 1
        captured.append(buffer)

    pump = threading.Thread(target=terminal, daemon=True)
    pump.start()
    try:
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
        os.close(master)
        pump.join(timeout=5)
    output = captured[0] if captured else ""
    assert process.returncode == 0, output[-2000:]
    return json.loads(result_path.read_text(encoding="utf-8")), output


def test_calibrates_to_an_xtermjs_unicode6_terminal(tmp_path):
    result, output = _run_app_in_pty(xtermjs_unicode6_cells, tmp_path)

    assert result == {"env": "8.0.0", "materialize_vs16": True, "planet_cells": 1, "robot_cells": 1}
    probes, _, frames = output.partition(PROBES_DONE)
    assert PROBE_RE.findall(probes) == list(PROBES)
    # Uma sondagem só, mesmo com o suspend/resume feito pela app.
    assert len(PROBE_RE.findall(output)) == len(PROBES)
    # O frame sai com o seletor materializado.
    assert "☁ " in frames
    assert "☁" + VS16 not in frames


def test_keeps_the_latest_table_and_the_selector_on_a_modern_terminal(tmp_path):
    result, output = _run_app_in_pty(table_cells(LATEST, honors_vs16=True), tmp_path)

    assert result == {"env": LATEST, "materialize_vs16": False, "planet_cells": 2, "robot_cells": 2}
    _, _, frames = output.partition(PROBES_DONE)
    assert "☁" + VS16 + " nuvem" in frames


def test_starts_with_rich_defaults_when_the_terminal_never_answers(tmp_path):
    result, output = _run_app_in_pty(None, tmp_path, probe_timeout=0.3)

    assert result == {"env": None, "materialize_vs16": True, "planet_cells": 2, "robot_cells": 2}
    _, _, frames = output.partition(PROBES_DONE)
    assert "☁ " in frames  # sem medida, o conservador é materializar


def test_quimera_textual_app_uses_the_calibrating_driver():
    captured_apps = []
    bridge = TextualUiBridge()
    quimera_app = SimpleNamespace(
        renderer=None,
        input_gate=SimpleNamespace(_build_toolbar_renderable=Mock(return_value="toolbar")),
    )

    with patch("textual.app.App.run", lambda app, *args, **kwargs: captured_apps.append(app)):
        run_textual_quimera_app(quimera_app, bridge)

    assert captured_apps[0].driver_class is QuimeraLinuxDriver
