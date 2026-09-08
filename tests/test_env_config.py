"""Tests for quimera.env_config."""
import os
from pathlib import Path

import pytest

from quimera.env_config import EnvConfig


@pytest.fixture
def env_file(tmp_path) -> Path:
    """Caminho para o arquivo .env dentro de um diretório descartável."""
    return tmp_path / ".env"


_LOAD_CASES = [
    pytest.param(None, {}, id="sem-arquivo"),
    pytest.param("API_KEY=secret\n", {"API_KEY": "secret"}, id="arquivo-basico"),
    pytest.param(
        "\n# comment\nAPI_KEY=secret\n\n  # another comment\nMODEL=gpt-5\n",
        {"API_KEY": "secret", "MODEL": "gpt-5"},
        id="ignora-comentarios-e-brancos",
    ),
    pytest.param('CHATGPT_KEY="sk-abc123"\n', {"CHATGPT_KEY": "sk-abc123"}, id="aspas-duplas"),
    pytest.param("CHATGPT_KEY='sk-abc123'\n", {"CHATGPT_KEY": "sk-abc123"}, id="aspas-simples"),
    pytest.param("TOKEN=abc=def=ghi\n", {"TOKEN": "abc=def=ghi"}, id="valor-com-igual"),
    pytest.param("TOKEN=\"value'\n", {"TOKEN": "\"value'"}, id="aspas-desiguais"),
]


@pytest.mark.parametrize(("content", "expected"), _LOAD_CASES)
def test_load(env_file, content, expected):
    """_load parseia o conteúdo do arquivo (ou dict vazio se não existir)."""
    if content is not None:
        env_file.write_text(content, encoding="utf-8")
    assert EnvConfig(env_file)._load() == expected


def test_get_existing_key(env_file):
    """get retorna o valor de uma chave existente."""
    env_file.write_text("MODEL=gpt-5\n", encoding="utf-8")
    assert EnvConfig(env_file).get("MODEL") == "gpt-5"


def test_get_missing_key_returns_default(env_file):
    """get retorna o default para chave inexistente."""
    assert EnvConfig(env_file).get("MISSING", "fallback") == "fallback"


def test_set_creates_file(env_file):
    """set cria o arquivo .env quando ele ainda não existe."""
    env = EnvConfig(env_file)
    env.set("API_KEY", "secret")
    assert env_file.exists()
    assert env_file.read_text(encoding="utf-8") == "API_KEY=secret\n"


def test_set_updates_existing_key(env_file):
    """set atualiza apenas a chave-alvo, preservando as demais."""
    env_file.write_text("API_KEY=old\nMODEL=gpt-5\n", encoding="utf-8")
    env = EnvConfig(env_file)
    env.set("API_KEY", "new")
    assert env.all() == {"API_KEY": "new", "MODEL": "gpt-5"}


def test_delete_removes_key(env_file):
    """delete remove a chave-alvo, preservando as demais."""
    env_file.write_text("API_KEY=secret\nMODEL=gpt-5\n", encoding="utf-8")
    env = EnvConfig(env_file)
    env.delete("API_KEY")
    assert env.all() == {"MODEL": "gpt-5"}


def test_delete_missing_key_is_noop(env_file):
    """delete de chave inexistente não altera o estado."""
    env_file.write_text("MODEL=gpt-5\n", encoding="utf-8")
    env = EnvConfig(env_file)
    env.delete("API_KEY")
    assert env.all() == {"MODEL": "gpt-5"}


def test_all_returns_copy(env_file):
    """all devolve uma cópia; mutações na cópia não afetam o estado."""
    env_file.write_text("MODEL=gpt-5\n", encoding="utf-8")
    env = EnvConfig(env_file)
    data = env.all()
    data["MODEL"] = "changed"
    assert env.all() == {"MODEL": "gpt-5"}


def test_apply_to_environ_uses_setdefault(env_file, monkeypatch):
    """apply_to_environ respeita variáveis já definidas no ambiente."""
    env_file.write_text("MODEL=from-file\nAPI_KEY=secret\n", encoding="utf-8")
    monkeypatch.setenv("MODEL", "existing")
    monkeypatch.delenv("API_KEY", raising=False)

    EnvConfig(env_file).apply_to_environ()

    assert os.environ["MODEL"] == "existing"
    assert os.environ["API_KEY"] == "secret"


def test_setenv_persists_and_updates_environ(env_file, monkeypatch):
    """setenv persiste no arquivo e atualiza os.environ imediatamente."""
    monkeypatch.delenv("API_KEY", raising=False)
    env = EnvConfig(env_file)

    env.setenv("API_KEY", "secret")

    assert env_file.read_text(encoding="utf-8") == "API_KEY=secret\n"
    assert os.environ["API_KEY"] == "secret"