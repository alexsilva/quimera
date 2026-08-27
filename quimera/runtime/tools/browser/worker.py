"""Isolated browser worker entrypoint.

This module intentionally uses subprocess inheritance instead of
``multiprocessing.spawn``. Long-lived Quimera hosts may inherit a stale
``multiprocessing.resource_tracker`` file descriptor; ``spawn`` can then feed
duplicate descriptors to ``_posixsubprocess`` and fail before Playwright starts.
The browser worker needs only one explicitly inherited socket, so a plain
subprocess gives us the same killable process boundary without that global
runtime dependency.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

# Executado por caminho absoluto para não depender do cwd do host. O pai do
# pacote ``quimera`` funciona tanto no checkout quanto em uma instalação normal.
package_parent = Path(__file__).resolve().parents[4]
if str(package_parent) not in sys.path:
    sys.path.insert(0, str(package_parent))

from quimera.runtime.tools.browser.service import BrowserService, _SocketConnection


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    fd = int(sys.argv[1])
    workspace_root = Path(sys.argv[2])
    channel = socket.socket(fileno=fd)
    service = BrowserService(workspace_root, _worker_process=True)
    service._serve_process(_SocketConnection(channel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
