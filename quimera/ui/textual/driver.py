"""Driver Textual do Quimera: calibra as larguras de célula no terminal real.

Ao entrar em modo de aplicação, antes do primeiro frame, o driver escreve cada
item de ``cell_widths.PROBES`` na coluna 1 seguido de um pedido de posição do
cursor (``CSI 6 n``): a coluna em que o cursor parou é o número de células que
o terminal usou — o mesmo recurso com que o Vim descobre larguras ambíguas. As
respostas chegam pela thread de input já convertidas em
``events.CursorPosition`` pelo parser do Textual e são interceptadas em
``process_message``, como o ``LinuxDriver`` faz com ``InBandWindowResize``.

Com as medidas, ``cell_widths.calibrate`` ativa a tabela do Rich que reproduz
o terminal. Se o terminal ignora o U+FE0F, a saída é normalizada em ``write``
— o único ponto por onde o Textual escreve, o que cobre widgets próprios,
nativos (paleta de comandos, Input) e texto vindo dos agentes.
"""
from __future__ import annotations

import queue
import time
from typing import Sequence

from textual import events
from textual.drivers.linux_driver import LinuxDriver
from textual.message import Message

from quimera.ui.textual.cell_widths import PROBES, calibrate, materialize_variation_selectors

_CURSOR_HOME = "\x1b[1;1H"
_REPORT_CURSOR_POSITION = "\x1b[6n"
_CLEAR_LINE = "\x1b[2K"


class QuimeraLinuxDriver(LinuxDriver):
    """``LinuxDriver`` que mede o terminal na partida e normaliza a saída."""

    probe_timeout = 1.0
    """Segundos de espera pelas respostas do terminal à sondagem."""

    _calibrated = False
    _materialize_vs16 = True  # até a sondagem mostrar que o terminal honra o seletor
    _cursor_reports: queue.Queue[int] | None = None

    def start_application_mode(self) -> None:
        super().start_application_mode()
        # Uma vez por driver: resume_application_mode() volta a passar aqui.
        if self._writer_thread is not None and not self._calibrated:
            self._calibrated = True
            self._calibrate()

    def _calibrate(self) -> None:
        if not self.input_tty:
            return
        terminal = calibrate(self._measure(PROBES))
        self._materialize_vs16 = terminal is None or not terminal.honors_vs16

    def _measure(self, texts: Sequence[str]) -> list[int]:
        """Células que o terminal usou para cada texto, até ``probe_timeout``."""
        reports = self._cursor_reports = queue.Queue()
        deadline = time.monotonic() + self.probe_timeout
        widths: list[int] = []
        try:
            # Direto ao terminal: a sonda do U+FE0F não pode ser materializada.
            super().write("".join(f"{_CURSOR_HOME}{text}{_REPORT_CURSOR_POSITION}" for text in texts))
            self.flush()
            for _ in texts:
                widths.append(reports.get(timeout=max(0.0, deadline - time.monotonic())))
        except queue.Empty:
            pass
        finally:
            self._cursor_reports = None
            super().write(_CURSOR_HOME + _CLEAR_LINE)
            self.flush()
        return widths

    def process_message(self, message: Message) -> None:
        reports = self._cursor_reports
        if reports is not None and isinstance(message, events.CursorPosition):
            reports.put(message.x)  # coluna − 1: células que o texto ocupou
            return
        super().process_message(message)

    def write(self, data: str) -> None:
        if self._materialize_vs16:
            data = materialize_variation_selectors(data)
        super().write(data)
