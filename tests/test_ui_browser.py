"""Abertura de URLs no navegador sem tocar no terminal da TUI."""
from __future__ import annotations

import pytest

from quimera.ui import browser
from quimera.ui.browser import browser_available, open_in_browser


@pytest.fixture
def popen_calls(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return object()

    monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(browser.sys, "platform", "linux")
    return calls


def test_sem_sessao_grafica_nao_abre_nada(popen_calls):
    env = {"PATH": "/usr/bin"}

    assert browser_available(env) is False
    assert open_in_browser("https://auth.example.test/x", env) is False
    assert popen_calls == []


def test_kill_switch_por_ambiente(popen_calls):
    env = {"DISPLAY": ":0", "QUIMERA_OPEN_BROWSER": "0"}

    assert browser_available(env) is False
    assert open_in_browser("https://auth.example.test/x", env) is False
    assert popen_calls == []


def test_abre_com_xdg_open_desacoplado_do_terminal(popen_calls, monkeypatch):
    monkeypatch.setattr(browser.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "xdg-open" else None)
    env = {"DISPLAY": ":0"}

    assert open_in_browser("https://auth.example.test/x?a=1&b=2", env) is True

    (cmd, kwargs), = popen_calls
    assert cmd == ["xdg-open", "https://auth.example.test/x?a=1&b=2"]
    assert kwargs["stdin"] is browser.subprocess.DEVNULL
    assert kwargs["stdout"] is browser.subprocess.DEVNULL
    assert kwargs["stderr"] is browser.subprocess.DEVNULL
    assert kwargs["start_new_session"] is True


def test_respeita_variavel_browser(popen_calls):
    env = {"WAYLAND_DISPLAY": "wayland-0", "BROWSER": "firefox --new-tab %s"}

    assert open_in_browser("https://auth.example.test/x", env) is True

    assert popen_calls[0][0] == ["firefox", "--new-tab", "https://auth.example.test/x"]

    env = {"WAYLAND_DISPLAY": "wayland-0", "BROWSER": "chromium"}
    assert open_in_browser("https://auth.example.test/y", env) is True
    assert popen_calls[1][0] == ["chromium", "https://auth.example.test/y"]


def test_recusa_urls_que_nao_sao_http_e_falhas_do_lancador(popen_calls, monkeypatch):
    env = {"DISPLAY": ":0"}
    assert open_in_browser("file:///etc/passwd", env) is False
    assert open_in_browser("", env) is False
    assert popen_calls == []

    monkeypatch.setattr(browser.shutil, "which", lambda name: None)
    assert open_in_browser("https://auth.example.test/x", env) is False

    monkeypatch.setattr(browser.shutil, "which", lambda name: "/usr/bin/xdg-open")

    def boom(cmd, **kwargs):
        raise OSError("sem permissão")

    monkeypatch.setattr(browser.subprocess, "Popen", boom)
    assert open_in_browser("https://auth.example.test/x", env) is False
