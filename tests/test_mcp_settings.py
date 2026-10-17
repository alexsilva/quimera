"""Catálogo MCP compartilhado, overlays e preservação do formato anterior."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from quimera.config import ConfigManager
from quimera.runtime.mcp.client import MCPClientBridge, MCPConnectionPhase, start_mcp_clients
from quimera.runtime.mcp.manager import MCPConnectionManager
from quimera.runtime.mcp.settings import (
    SCOPE_GLOBAL,
    SCOPE_LOCAL,
    MCPConnectionStore,
    MCPHttpSettings,
    MCPHttpSettingsStore,
)
from quimera.runtime.registry import ToolRegistry
from quimera.runtime.tools.mcp_clients import set_bridge


@pytest.fixture
def store(tmp_path):
    return MCPConnectionStore(
        tmp_path / "global" / "mcp-connections.json",
        tmp_path / "workspace" / "mcp-connections.json",
    )


def test_catalog_is_inactive_and_legacy_connections_remain_active(store):
    store.global_config.set_mcp_configuration(
        ["github=stdio:github", "jira=stdio:jira"], ["github=TOKEN=global"]
    )
    store.workspace_config.set_mcp_configuration(
        ["ide=http://localhost:43129/mcp"], ["github=TOKEN=workspace"]
    )
    before = store.workspace_file.read_bytes()

    entries = {entry.name: entry for entry in store.entries()}

    assert entries["github"].is_global and not entries["github"].active
    assert entries["github"].env_text == "TOKEN=workspace"
    assert entries["ide"].scope == "local" and entries["ide"].active
    assert store.active_specs() == (["ide=http://localhost:43129/mcp"], [])
    assert store.workspace_file.read_bytes() == before
    store.set_active("github", True)
    assert store.active_specs() == (
        ["github=stdio:github", "ide=http://localhost:43129/mcp"],
        ["github=TOKEN=workspace"],
    )
    store.set_active("github", False)
    assert store.entry("github").env_text == "TOKEN=workspace"
    # Uma local desliga só aqui; não tem padrão global para decidir em todos.
    assert not store.set_active("ide", False).active
    assert store.workspace_config.mcp_inactive == ["github", "ide"]
    with pytest.raises(ValueError, match="padrão global"):
        store.set_active("ide", True, SCOPE_GLOBAL)


def test_normalize_preserves_credentials_conflicts_and_existing_activation(store):
    store.global_config.set_mcp_configuration(
        ["github=stdio:github", "ide=http://localhost:43129/mcp", "jira=stdio:jira"],
        ["github=TOKEN=secret=with=equals,DISPLAY=:99", "jira=TOKEN=global"],
    )
    store.workspace_config.set_mcp_configuration(
        ["github=stdio:github", "ide=http://localhost:43135/mcp", "jira=stdio:jira"],
        ["github=DISPLAY=:99,TOKEN=secret=with=equals", "jira=TOKEN=local"],
    )
    store.workspace_config.set_mcp_active(["already-active"])
    global_before = store.global_file.read_bytes()

    assert store.normalize() == ["github"]
    assert store.workspace_config.mcp_active == ["already-active", "github"]
    assert store.entry("github").env_text == "TOKEN=secret=with=equals,DISPLAY=:99"
    assert store.entry("ide").spec == "ide=http://localhost:43135/mcp"
    assert store.entry("jira").env_text == "TOKEN=local"
    overlay_before = store.workspace_file.read_bytes()
    assert store.normalize() == []
    assert store.workspace_file.read_bytes() == overlay_before
    assert store.global_file.read_bytes() == global_before


def test_global_promotion_keeps_environment_and_activation_without_touching_the_default(store, tmp_path):
    """Salvar no global é configuração: entra no catálogo, mantém o estado daqui e não liga em todos."""
    store.upsert("github=stdio:github", env_spec="github=TOKEN=local-secret")

    entry = store.upsert("github=stdio:github", scope="global")

    assert entry.is_global and entry.env_text == "TOKEN=local-secret"
    # Era uma local ativa: segue ativa aqui, agora por exceção local, e o padrão global fica desligado.
    assert (entry.active, entry.activation_scope, entry.default_active) == (True, SCOPE_LOCAL, False)
    assert store.workspace_config.mcp_clients is None
    assert store.workspace_config.mcp_client_env is None
    assert store.workspace_config.mcp_active == ["github"]
    assert store.global_config.mcp_active is None
    other = MCPConnectionStore(store.global_file, tmp_path / "other" / "mcp-connections.json")
    assert other.entry("github").env_text == "TOKEN=local-secret"
    assert (other.entry("github").active, other.entry("github").activation_scope) == (False, SCOPE_GLOBAL)
    assert not other.workspace_file.exists()
    assert store.global_file.stat().st_mode & 0o777 == 0o600


def test_renaming_carries_activation_and_drops_the_old_name(store, tmp_path):
    store.upsert("gh=stdio:gh", env_spec="gh=TOKEN=x", scope=SCOPE_GLOBAL)
    store.set_active("gh", True, SCOPE_GLOBAL)
    store.set_active("gh", False, SCOPE_LOCAL)

    entry = store.upsert("github=stdio:gh", scope=SCOPE_GLOBAL, replaces="gh")

    assert store.entry("gh") is None
    assert entry.env_text == "TOKEN=x"
    # Padrão global ligado e exceção desligada daqui acompanham o nome novo.
    assert (entry.active, entry.activation_scope, entry.default_active) == (False, SCOPE_LOCAL, True)
    assert store.global_config.mcp_active == ["github"]
    assert store.global_config.mcp_client_env == ["github=TOKEN=x"]
    assert store.workspace_config.mcp_inactive == ["github"]
    other = MCPConnectionStore(store.global_file, tmp_path / "other" / "mcp-connections.json")
    assert other.entry("github").active and other.entry("gh") is None

    # Renomear uma local ativa para o escopo local segue ativa, sem exceção redundante.
    store.upsert("ide=stdio:ide")
    entry = store.upsert("editor=stdio:ide", replaces="ide")
    assert store.entry("ide") is None and entry.active and entry.scope == SCOPE_LOCAL
    assert store.workspace_config.mcp_clients == ["editor=stdio:ide"]
    assert store.workspace_config.mcp_active is None


def test_clearing_local_environment_does_not_reintroduce_global_credentials(store):
    store.upsert("github=stdio:github", env_spec="github=TOKEN=global", scope="global")

    entry = store.upsert("github=stdio:github", env_spec="", scope="local")

    assert entry.scope == "local" and entry.env_text == ""
    assert store.workspace_config.mcp_client_env == ["github="]
    assert store.normalize() == []
    assert store.entry("github").env_text == ""
    assert store.global_config.mcp_client_env == ["github=TOKEN=global"]
    # A global nunca foi ligada aqui: o override nasceu desligado e, removido, ela segue desligada.
    assert not store.entry("github").active
    restored = store.remove("github")
    assert restored.is_global and not restored.active
    assert restored.env_text == "TOKEN=global"


def test_rename_rejects_a_name_already_in_use(store):
    store.upsert("alpha=stdio:alpha")
    store.upsert("beta=stdio:beta", env_spec="beta=TOKEN=b")

    with pytest.raises(ValueError, match="Já existe"):
        store.upsert("beta=stdio:renamed-alpha", replaces="alpha")

    assert store.entry("alpha").spec == "alpha=stdio:alpha"
    assert (store.entry("beta").spec, store.entry("beta").env_text) == ("beta=stdio:beta", "TOKEN=b")


def test_catalog_rename_and_removal_follow_the_name_in_every_workspace(store, tmp_path):
    other = MCPConnectionStore(store.global_file, tmp_path / "other" / "mcp-connections.json")
    third = MCPConnectionStore(store.global_file, tmp_path / "third" / "mcp-connections.json")
    store.upsert("gh=stdio:gh", env_spec="gh=TOKEN=shared", scope=SCOPE_GLOBAL)
    store.set_active("gh", True, SCOPE_GLOBAL)
    other.set_active("gh", False, SCOPE_LOCAL)
    other.workspace_config.set_mcp_configuration(None, ["gh=TOKEN=other"])
    third.upsert("gh=stdio:gh-third")

    entry = store.upsert("github=stdio:gh", scope=SCOPE_GLOBAL, replaces="gh")

    # Quem renomeou segue o padrão; a exceção e o ambiente de other acompanham o nome novo.
    assert (entry.active, entry.activation_scope) == (True, SCOPE_GLOBAL)
    renamed = other.entry("github")
    assert (renamed.active, renamed.activation_scope, renamed.env_text) == (False, SCOPE_LOCAL, "TOKEN=other")
    assert other.workspace_config.mcp_inactive == ["github"]
    assert other.workspace_config.mcp_client_env == ["github=TOKEN=other"]
    assert other.entry("gh") is None
    # O override local de third é conexão dele: fica com o nome antigo, e a global nova aparece ao lado.
    assert (third.entry("gh").spec, third.entry("gh").scope) == ("gh=stdio:gh-third", SCOPE_LOCAL)
    assert third.entry("github").is_global and third.entry("github").active

    # Remover do catálogo apaga pins e ambiente herdado em todos os workspaces; o override de third fica.
    other.set_active("github", True, SCOPE_LOCAL)
    assert store.remove("github") is None
    assert other.workspace_config.mcp_active is None and other.workspace_config.mcp_client_env is None
    assert third.entry("gh").spec == "gh=stdio:gh-third"
    # Recriar o nome nasce desligada em todos: nenhuma decisão antiga dita o estado.
    store.upsert("github=stdio:new", scope=SCOPE_GLOBAL)
    assert not other.entry("github").active and not store.entry("github").active


def test_catalog_rename_handles_peer_references_to_the_new_name(store, tmp_path):
    peer = MCPConnectionStore(store.global_file, tmp_path / "peer" / "mcp-connections.json")
    local_peer = MCPConnectionStore(store.global_file, tmp_path / "local_peer" / "mcp-connections.json")
    store.upsert("old=stdio:old", scope=SCOPE_GLOBAL)
    peer.workspace_file.parent.mkdir()
    # ``old`` ligada só em peer, com ambiente próprio; ``new`` tem pin e ambiente órfãos ali.
    peer.workspace_file.write_text(json.dumps({
        "mcp_active": ["old"],
        "mcp_inactive": ["new"],
        "mcp_client_env": ["old=TOKEN=peer", "new=TOKEN=orphan"],
    }))
    local_peer.upsert("new=stdio:mine")
    local_peer.set_active("old", False, SCOPE_LOCAL)
    orphan_peer = MCPConnectionStore(store.global_file, tmp_path / "orphan_peer" / "mcp-connections.json")
    orphan_peer.workspace_file.parent.mkdir()
    orphan_peer.workspace_file.write_text(
        json.dumps({"mcp_active": ["new"], "mcp_client_env": ["new=TOKEN=orphan"]})
    )

    store.upsert("new=stdio:old", scope=SCOPE_GLOBAL, replaces="old")

    # Quem só tinha referências órfãs ao nome novo passa a seguir o padrão, sem credencial órfã.
    orphan = orphan_peer.entry("new")
    assert (orphan.active, orphan.activation_scope, orphan.env_text) == (False, SCOPE_GLOBAL, "")
    assert json.loads(orphan_peer.workspace_file.read_text()) == {}
    entry = peer.entry("new")
    assert (entry.active, entry.activation_scope, entry.env_text) == (True, SCOPE_LOCAL, "TOKEN=peer")
    assert peer.workspace_config.mcp_active == ["new"] and peer.workspace_config.mcp_inactive is None
    assert peer.workspace_config.mcp_client_env == ["new=TOKEN=peer"]
    # A conexão local ``new`` de local_peer segue como estava e as referências a ``old`` somem.
    mine = local_peer.entry("new")
    assert (mine.scope, mine.active, mine.spec) == (SCOPE_LOCAL, True, "new=stdio:mine")
    assert local_peer.workspace_config.mcp_inactive is None


def test_catalog_rename_rechecks_each_overlay_under_the_lock(store, tmp_path, monkeypatch):
    from quimera.runtime.mcp import settings as module

    peer = MCPConnectionStore(store.global_file, tmp_path / "peer" / "mcp-connections.json")
    store.upsert("old=stdio:old", env_spec="old=TOKEN=g", scope=SCOPE_GLOBAL)
    peer.set_active("old", False, SCOPE_LOCAL)
    peer.workspace_config.set_mcp_configuration(None, ["old=TOKEN=peer"])
    original = module.update_json_object

    def racing(path, update, after_write=None):
        if Path(path).resolve() == peer.workspace_file.resolve():
            # Outro processo grava uma spec local ``old`` entre o filtro e o lock.
            original(path, lambda data: data.__setitem__("mcp_clients", ["old=stdio:peer-local"]))
        return original(path, update, after_write=after_write)

    monkeypatch.setattr(module, "update_json_object", racing)
    store.upsert("new=stdio:old", scope=SCOPE_GLOBAL, replaces="old")

    local = peer.entry("old")
    assert (local.scope, local.active, local.env_text) == (SCOPE_LOCAL, False, "TOKEN=peer")
    assert peer.workspace_config.mcp_inactive == ["old"]
    assert peer.entry("new").is_global and not peer.entry("new").active


def test_catalog_rename_rejects_a_name_created_meanwhile(store, tmp_path, monkeypatch):
    from quimera.runtime.mcp import settings as module

    store.upsert("old=stdio:old", env_spec="old=TOKEN=o", scope=SCOPE_GLOBAL)
    store.set_active("old", True, SCOPE_LOCAL)
    original = module.update_json_object
    armed = {"on": True}

    def racing(path, update, after_write=None):
        if armed["on"] and Path(path).resolve() == store.global_file.resolve():
            armed["on"] = False
            # Outro workspace grava ``new`` entre a validação e o lock do catálogo.
            original(path, lambda data: data.__setitem__("mcp_clients", data["mcp_clients"] + ["new=stdio:concurrent"]))
        return original(path, update, after_write=after_write)

    monkeypatch.setattr(module, "update_json_object", racing)
    with pytest.raises(ValueError, match="Já existe"):
        store.upsert("new=stdio:old", scope=SCOPE_GLOBAL, replaces="old")

    # Nada foi sobrescrito: ``old`` segue inteira e ``new`` é a do outro workspace, sem decisão daqui.
    assert (store.entry("old").spec, store.entry("old").env_text, store.entry("old").active) == (
        "old=stdio:old", "TOKEN=o", True,
    )
    assert store.entry("new").spec == "new=stdio:concurrent" and not store.entry("new").active
    assert store.workspace_config.mcp_active == ["old"]


def test_catalog_rename_migrates_a_pin_whose_local_spec_vanished_before_the_lock(store, tmp_path, monkeypatch):
    from quimera.runtime.mcp import settings as module

    peer = MCPConnectionStore(store.global_file, tmp_path / "peer" / "mcp-connections.json")
    store.upsert("old=stdio:old", scope=SCOPE_GLOBAL)
    store.set_active("old", True, SCOPE_GLOBAL)
    peer.upsert("old=stdio:peer-local")
    peer.set_active("old", False, SCOPE_LOCAL)
    original = module.update_json_object

    def racing(path, update, after_write=None):
        if Path(path).resolve() == peer.workspace_file.resolve():
            # Outro processo remove a spec local ``old`` entre o filtro e o lock; o pin fica.
            original(path, lambda data: data.pop("mcp_clients", None))
        return original(path, update, after_write=after_write)

    monkeypatch.setattr(module, "update_json_object", racing)
    store.upsert("new=stdio:old", scope=SCOPE_GLOBAL, replaces="old")

    entry = peer.entry("new")
    assert (entry.is_global, entry.active, entry.activation_scope) == (True, False, SCOPE_LOCAL)
    assert peer.workspace_config.mcp_inactive == ["new"] and peer.entry("old") is None


def test_catalog_rename_keeps_activation_when_cleaning_the_old_name_fails(store, monkeypatch, caplog):
    from quimera.runtime.mcp import settings as module

    store.upsert("old=stdio:old", scope=SCOPE_GLOBAL)
    store.set_active("old", True, SCOPE_LOCAL)  # ligada só aqui; o padrão global não a liga
    original = module.update_json_object
    local_writes = []

    def failing_second_local_write(path, update, after_write=None):
        if Path(path).resolve() == store.workspace_file.resolve():
            local_writes.append(path)
            if len(local_writes) == 2:
                raise PermissionError("overlay deste workspace bloqueado")
        return original(path, update, after_write=after_write)

    monkeypatch.setattr(module, "update_json_object", failing_second_local_write)
    with caplog.at_level(logging.WARNING, logger="quimera.runtime.mcp.settings"):
        entry = store.upsert("new=stdio:old", scope=SCOPE_GLOBAL, replaces="old")

    # A decisão para o nome novo foi gravada antes do catálogo; só a limpeza do antigo falhou (e foi logada).
    assert (entry.active, entry.activation_scope) == (True, SCOPE_LOCAL)
    assert store.entry("old") is None and store.global_config.mcp_active is None
    assert store.workspace_config.mcp_active == ["old", "new"]
    assert "overlay deste workspace bloqueado" in caplog.text


def test_corrupt_catalog_rejects_the_save_without_touching_the_overlay(store):
    store.workspace_config.set_mcp_active(["github"])
    store.workspace_config.set_mcp_configuration(None, ["github=TOKEN=kept"])
    overlay_before = store.workspace_file.read_bytes()
    store.global_file.parent.mkdir()
    store.global_file.write_text("{invalid-json")

    with pytest.raises(ValueError, match="corrija o arquivo"):
        store.upsert("github=stdio:new", scope=SCOPE_GLOBAL)

    assert store.workspace_file.read_bytes() == overlay_before
    assert store.global_file.read_text() == "{invalid-json"


def test_publishing_a_name_keeps_decisions_taken_right_after_it(store, tmp_path, monkeypatch):
    from quimera.runtime.mcp import settings as module

    peer = MCPConnectionStore(store.global_file, tmp_path / "peer" / "mcp-connections.json")
    peer.workspace_file.parent.mkdir()
    peer.workspace_file.write_text(json.dumps({"mcp_active": ["github"]}))  # órfão
    original = module.update_json_object

    def peer_reacts(path, update, after_write=None):
        result = original(path, update, after_write=after_write)
        if Path(path).resolve() == store.global_file.resolve() and peer.entry("github") is not None:
            # Assim que o nome é publicado, o peer o liga por decisão própria.
            if not peer.entry("github").active:
                original(peer.workspace_file, lambda data: data.__setitem__("mcp_active", ["github"]))
        return result

    monkeypatch.setattr(module, "update_json_object", peer_reacts)
    entry = store.upsert("github=stdio:github", scope=SCOPE_GLOBAL)

    # O órfão saiu antes da publicação; a decisão tomada depois dela fica.
    assert not entry.active
    assert (peer.entry("github").active, peer.entry("github").activation_scope) == (True, SCOPE_LOCAL)

    # No rename, uma decisão do peer sobre o nome novo tomada após a publicação vence a transplantada.
    store.set_active("github", True, SCOPE_GLOBAL)
    peer.workspace_file.write_text(json.dumps({"mcp_active": ["github"]}))

    def peer_turns_new_off(path, update, after_write=None):
        result = original(path, update, after_write=after_write)
        if Path(path).resolve() == store.global_file.resolve() and peer.entry("gh") is not None:
            peer.set_active("gh", False, SCOPE_LOCAL)
        return result

    monkeypatch.setattr(module, "update_json_object", peer_turns_new_off)
    store.upsert("gh=stdio:github", scope=SCOPE_GLOBAL, replaces="github")
    assert not peer.entry("gh").active
    assert peer.workspace_config.mcp_active is None and peer.workspace_config.mcp_inactive == ["gh"]


def test_concurrent_publication_during_save_keeps_the_winner_and_its_peer_decisions(store, tmp_path, monkeypatch):
    from quimera.runtime.mcp import settings as module

    other = MCPConnectionStore(store.global_file, tmp_path / "other" / "mcp-connections.json")
    peer = MCPConnectionStore(store.global_file, tmp_path / "peer" / "mcp-connections.json")
    peer.workspace_file.parent.mkdir()
    peer.workspace_file.write_text(json.dumps({"mcp_active": ["github"]}))  # órfão, por enquanto
    original = module.update_json_object
    armed = {"on": True}

    def others_win_the_race(path, update, after_write=None):
        if armed["on"] and Path(path).resolve() == store.global_file.resolve():
            armed["on"] = False
            # Entre a leitura de A e o lock do catálogo: B publica o nome e o peer o liga de propósito.
            other.upsert("github=stdio:winner", scope=SCOPE_GLOBAL)
            peer.set_active("github", True, SCOPE_LOCAL)
        return original(path, update, after_write=after_write)

    monkeypatch.setattr(module, "update_json_object", others_win_the_race)
    with pytest.raises(ValueError, match="Já existe"):
        store.upsert("github=stdio:loser", scope=SCOPE_GLOBAL)

    assert store.entry("github").spec == "github=stdio:winner"
    assert (peer.entry("github").active, peer.entry("github").activation_scope) == (True, SCOPE_LOCAL)
    assert store.workspace_config.mcp_active is None


def test_losing_a_publication_race_keeps_this_workspace_decision(store, tmp_path, monkeypatch):
    from quimera.runtime.mcp import settings as module

    other = MCPConnectionStore(store.global_file, tmp_path / "other" / "mcp-connections.json")
    original = module.update_json_object
    armed = {"on": True}

    def others_win_and_we_decide(path, update, after_write=None):
        if armed["on"] and Path(path).resolve() == store.global_file.resolve():
            armed["on"] = False
            # B publica antes do nosso lock e este workspace já liga a conexão de B.
            other.upsert("github=stdio:winner", scope=SCOPE_GLOBAL)
            store.set_active("github", True, SCOPE_LOCAL)
        return original(path, update, after_write=after_write)

    monkeypatch.setattr(module, "update_json_object", others_win_and_we_decide)
    with pytest.raises(ValueError, match="Já existe"):
        store.upsert("github=stdio:loser", scope=SCOPE_GLOBAL)

    # A fase 1 nem rodou: nada a desfazer, e a decisão sobre a conexão de B fica.
    entry = store.entry("github")
    assert (entry.spec, entry.active, entry.activation_scope) == ("github=stdio:winner", True, SCOPE_LOCAL)
    assert store.workspace_config.mcp_active == ["github"]


def test_catalog_rename_and_removal_touch_overlays_under_the_catalog_lock(store, tmp_path, monkeypatch):
    from quimera.runtime.mcp import settings as module

    peer = MCPConnectionStore(store.global_file, tmp_path / "peer" / "mcp-connections.json")
    store.upsert("old=stdio:old", scope=SCOPE_GLOBAL)
    peer.set_active("old", False, SCOPE_LOCAL)
    original = module.update_json_object
    seen = []

    def spy(path, update, after_write=None):
        if Path(path).resolve() != store.global_file.resolve() or after_write is None:
            return original(path, update, after_write=after_write)

        def tracked():
            seen.append(("antes", peer.workspace_config.mcp_inactive))
            after_write()
            seen.append(("depois", peer.workspace_config.mcp_inactive))

        return original(path, update, after_write=tracked)

    monkeypatch.setattr(module, "update_json_object", spy)
    store.upsert("new=stdio:old", scope=SCOPE_GLOBAL, replaces="old")
    assert seen == [("antes", ["old"]), ("depois", ["new"])]
    seen.clear()
    assert store.remove("new") is None
    assert seen == [("antes", ["new"]), ("depois", None)]
    assert peer.entry("new") is None and not peer.workspace_file.exists() or peer.workspace_config.mcp_inactive is None

    # Spec trocada entre a leitura e o lock: a remoção é recusada e a conexão nova fica.
    store.upsert("github=stdio:v1", scope=SCOPE_GLOBAL)
    armed = {"on": True}

    def republish(path, update, after_write=None):
        if armed["on"] and Path(path).resolve() == store.global_file.resolve():
            armed["on"] = False
            original(path, lambda data: data.__setitem__("mcp_clients", ["github=stdio:v2"]))
        return original(path, update, after_write=after_write)

    monkeypatch.setattr(module, "update_json_object", republish)
    with pytest.raises(ValueError, match="mudou no catálogo"):
        store.remove("github")
    assert store.entry("github").spec == "github=stdio:v2"


def test_rename_tolerates_overlay_discovery_failure_after_publication(store, monkeypatch, caplog):
    store.upsert("old=stdio:old", scope=SCOPE_GLOBAL)
    store.set_active("old", True, SCOPE_LOCAL)
    original = MCPConnectionStore._sibling_overlays
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 2:  # a primeira (purga pré-publicação) passa; a do pós-publicação falha
            raise PermissionError("descoberta bloqueada")
        return original(self)

    monkeypatch.setattr(MCPConnectionStore, "_sibling_overlays", flaky)
    with caplog.at_level(logging.WARNING, logger="quimera.runtime.mcp.settings"):
        entry = store.upsert("new=stdio:old", scope=SCOPE_GLOBAL, replaces="old")

    # Publicado, nada é desfeito: a ativação do nome novo fica e o aviso vai ao log.
    assert (entry.active, entry.activation_scope) == (True, SCOPE_LOCAL)
    assert store.entry("old") is None and calls["n"] >= 2
    assert "descoberta bloqueada" in caplog.text


def test_rename_survives_a_malformed_peer_overlay(store, tmp_path, caplog):
    peer = MCPConnectionStore(store.global_file, tmp_path / "peer" / "mcp-connections.json")
    broken = MCPConnectionStore(store.global_file, tmp_path / "broken" / "mcp-connections.json")
    store.upsert("old=stdio:old", scope=SCOPE_GLOBAL)
    store.set_active("old", True, SCOPE_LOCAL)
    peer.workspace_file.parent.mkdir()
    peer.workspace_file.write_text(json.dumps({"mcp_client_env": ["old"], "mcp_active": ["old"]}))
    broken.workspace_file.parent.mkdir()
    broken.workspace_file.write_text(json.dumps({"mcp_active": "old"}))  # tipo errado: ignorado por _specs

    with caplog.at_level(logging.WARNING, logger="quimera.runtime.mcp.settings"):
        entry = store.upsert("new=stdio:old", scope=SCOPE_GLOBAL, replaces="old")

    assert (entry.active, entry.activation_scope) == (True, SCOPE_LOCAL) and store.entry("old") is None
    # Entrada de ambiente sem "=" migra como ambiente vazio, sem derrubar a operação.
    assert peer.workspace_config.mcp_client_env == ["new="] and peer.workspace_config.mcp_active == ["new"]
    assert peer.entry("new").active and peer.entry("new").env_text == ""
    assert json.loads(broken.workspace_file.read_text()) == {"mcp_active": "old"}


def test_promoting_keeps_a_local_override_saved_after_publication(store, monkeypatch):
    from quimera.runtime.mcp import settings as module

    store.upsert("github=stdio:local-v1", env_spec="github=TOKEN=v1")
    original = module.update_json_object

    def another_process_saves_locally(path, update, after_write=None):
        if Path(path).resolve() != store.global_file.resolve() or after_write is None:
            return original(path, update, after_write=after_write)

        def interleaved():
            # Outro processo deste workspace grava um override novo logo após a publicação.
            original(store.workspace_file, lambda data: data.__setitem__("mcp_clients", ["github=stdio:local-v2"]))
            after_write()

        return original(path, update, after_write=interleaved)

    monkeypatch.setattr(module, "update_json_object", another_process_saves_locally)
    store.upsert("github=stdio:global", env_spec="github=TOKEN=v1", scope=SCOPE_GLOBAL)

    # O override que estava aqui ao ler (v1 e o ambiente) saiu; o gravado depois (v2) é de quem o gravou e fica.
    entry = store.entry("github")
    assert (entry.spec, entry.scope, entry.active) == ("github=stdio:local-v2", SCOPE_LOCAL, True)
    assert store.workspace_config.mcp_clients == ["github=stdio:local-v2"]
    assert store.workspace_config.mcp_client_env is None
    assert store.global_config.mcp_clients == ["github=stdio:global"]


def test_catalog_rename_keeps_a_peer_decision_for_the_new_name_over_the_transplanted_one(store, tmp_path, monkeypatch):
    from quimera.runtime.mcp import settings as module

    peer = MCPConnectionStore(store.global_file, tmp_path / "peer" / "mcp-connections.json")
    store.upsert("old=stdio:old", scope=SCOPE_GLOBAL)
    store.set_active("old", True, SCOPE_GLOBAL)
    peer.set_active("old", False, SCOPE_LOCAL)
    original = module.update_json_object

    def peer_turns_new_on(path, update, after_write=None):
        result = original(path, update, after_write=after_write)
        if Path(path).resolve() == store.global_file.resolve() and peer.entry("new") is not None:
            peer.set_active("new", True, SCOPE_LOCAL)
        return result

    monkeypatch.setattr(module, "update_json_object", peer_turns_new_on)
    store.upsert("new=stdio:old", scope=SCOPE_GLOBAL, replaces="old")

    assert (peer.entry("new").active, peer.entry("new").activation_scope) == (True, SCOPE_LOCAL)
    assert peer.workspace_config.mcp_active == ["new"] and peer.workspace_config.mcp_inactive is None


def test_catalog_rename_is_one_write_and_survives_a_failing_overlay(store, tmp_path, monkeypatch, caplog):
    from quimera.runtime.mcp import settings as module

    peer_ok = MCPConnectionStore(store.global_file, tmp_path / "a" / "mcp-connections.json")
    peer_bad = MCPConnectionStore(store.global_file, tmp_path / "b" / "mcp-connections.json")
    store.upsert("old=stdio:old", scope=SCOPE_GLOBAL)
    store.set_active("old", True, SCOPE_GLOBAL)
    peer_ok.set_active("old", False, SCOPE_LOCAL)
    peer_bad.set_active("old", False, SCOPE_LOCAL)
    original = module.update_json_object
    writes = []

    def failing(path, update, after_write=None):
        writes.append(Path(path).resolve())
        if Path(path).resolve() == peer_bad.workspace_file.resolve():
            raise PermissionError("overlay bloqueado")
        return original(path, update, after_write=after_write)

    monkeypatch.setattr(module, "update_json_object", failing)
    with caplog.at_level(logging.WARNING, logger="quimera.runtime.mcp.settings"):
        entry = store.upsert("new=stdio:old", scope=SCOPE_GLOBAL, replaces="old")

    assert entry.is_global and entry.active
    assert store.entry("old") is None and store.global_config.mcp_active == ["new"]
    assert writes.count(store.global_file.resolve()) == 1
    assert not peer_ok.entry("new").active
    # O overlay que falhou fica como estava: perde a exceção (segue o padrão) e o aviso vai ao log.
    assert peer_bad.entry("new").active and peer_bad.workspace_config.mcp_inactive == ["old"]
    assert "overlay bloqueado" in caplog.text


def test_new_connection_ignores_orphan_decisions(store, tmp_path):
    store.workspace_file.parent.mkdir()
    store.workspace_file.write_text(json.dumps({"mcp_active": ["github"], "mcp_inactive": ["ide"]}))
    peer = MCPConnectionStore(store.global_file, tmp_path / "peer" / "mcp-connections.json")
    peer.workspace_file.parent.mkdir()
    peer.workspace_file.write_text(json.dumps({"mcp_active": ["github"], "mcp_client_env": ["github=TOKEN=x"]}))

    entry = store.upsert("github=stdio:github", scope=SCOPE_GLOBAL)
    assert not entry.active and store.workspace_config.mcp_active is None
    # Uma global nova nasce desligada em todos: órfãos nos outros overlays também saem.
    assert not peer.entry("github").active and peer.entry("github").env_text == ""
    assert json.loads(peer.workspace_file.read_text()) == {}

    entry = store.upsert("ide=stdio:ide")
    assert entry.active and store.workspace_config.mcp_inactive is None


def test_identical_local_save_uses_catalog_and_removal_preserves_other_entries(store):
    store.upsert("github=stdio:github", env_spec="github=TOKEN=global", scope="global")
    store.upsert("ide=http://localhost:43129/mcp")
    entry = store.upsert("github=stdio:github", env_spec="github=TOKEN=global")
    # Cópia idêntica não vira override nem ativação: a global continua desligada aqui.
    assert entry.is_global and not entry.active
    assert store.workspace_config.mcp_clients == ["ide=http://localhost:43129/mcp"]
    assert store.workspace_config.mcp_active is None
    assert store.remove("github") is None
    assert store.entry("github") is None
    assert store.entry("ide").active
    assert store.workspace_config.mcp_active is None


def test_parallel_catalog_edits_preserve_every_connection_and_environment(store, tmp_path):
    def save(index):
        peer = MCPConnectionStore(store.global_file, tmp_path / str(index) / "mcp-connections.json")
        peer.upsert(f"server{index}=stdio:cmd{index}", env_spec=f"server{index}=TOKEN={index}", scope="global")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(24)))

    entries = {entry.name: entry for entry in store.entries()}
    assert len(entries) == 24
    assert all(entries[f"server{i}"].env_text == f"TOKEN={i}" for i in range(24))


def test_invalid_catalog_is_preserved_when_saving(store):
    store.global_file.parent.mkdir()
    store.global_file.write_text("{invalid-json")
    with pytest.raises(ValueError, match="corrija o arquivo"):
        store.upsert("github=stdio:github", scope="global")
    assert store.global_file.read_text() == "{invalid-json"
    assert not store.workspace_file.exists()


def test_single_file_legacy_store_is_never_absorbed_into_itself(tmp_path):
    path = tmp_path / "mcp-connections.json"
    legacy = MCPConnectionStore(path, path)
    legacy.upsert("github=stdio:github", env_spec="github=TOKEN=kept")
    before = path.read_bytes()
    assert legacy.normalize() == []
    assert legacy.active_specs() == (["github=stdio:github"], ["github=TOKEN=kept"])
    assert path.read_bytes() == before


def test_boot_uses_only_active_catalog_entries_and_legacy_locals(store):
    store.global_config.set_mcp_configuration(["github=stdio:github", "jira=stdio:jira"], [])
    store.workspace_config.set_mcp_configuration(["ide=stdio:ide"], [])
    store.set_active("github", True)
    try:
        runtime = start_mcp_clients(cli_specs=None, cli_env_specs=None, store=store)
        assert runtime.specs == ("github=stdio:github", "ide=stdio:ide")
        assert set(runtime.bridge.states()) == {"github", "ide"}
        assert runtime.bridge.sessions == {}
    finally:
        set_bridge(None)


def test_http_settings_resolve_per_key_and_explicit_false_overrides_global(tmp_path):
    store = MCPHttpSettingsStore(tmp_path / "global.json", tmp_path / "local.json")
    assert store.load() == MCPHttpSettings()
    store.save("global", host="0.0.0.0", port=9096, allow_tools="agent")
    store.save("local", enabled=False, port=9097)
    assert store.load() == MCPHttpSettings(False, "0.0.0.0", 9097, "agent")
    assert store.sources() == dict(enabled="local", host="global", port="local", allow_tools="global")
    store.save("local", port=None)
    assert store.load().port == 9096
    assert json.loads(store.workspace_file.read_text()) == {"http_enabled": False}


def test_http_server_is_never_enabled_by_the_global_layer(tmp_path):
    """Ligar o servidor é do workspace: um padrão global não faz todo Quimera disputar a porta."""
    store = MCPHttpSettingsStore(tmp_path / "global.json", tmp_path / "local.json")
    for enabled in (True, False):
        with pytest.raises(ValueError, match="workspace"):
            store.save("global", enabled=enabled)
    assert not store.global_file.exists()

    # Um arquivo global editado à mão também não liga o servidor nem aparece como origem.
    store.global_file.write_text('{"http_enabled": true, "http_port": 9096}', encoding="utf-8")
    assert store.load() == MCPHttpSettings(enabled=False, port=9096)
    assert store.sources()["enabled"] == "default"
    store.save("local", enabled=True)
    assert store.load().enabled and store.sources()["enabled"] == "local"
    # Apagar a chave residual do global continua permitido: não liga nada.
    store.save("global", enabled=None)
    assert json.loads(store.global_file.read_text(encoding="utf-8")) == {"http_port": 9096}


@pytest.mark.parametrize("values", [{"port": 0}, {"port": 65536}, {"port": True}, {"enabled": "false"}, {"host": " "}])
def test_http_settings_reject_invalid_values_without_writing(tmp_path, values):
    store = MCPHttpSettingsStore(tmp_path / "global.json", tmp_path / "local.json")
    with pytest.raises(ValueError):
        store.save("local", **values)
    assert not store.workspace_file.exists()


def test_connection_store_from_workspace_accepts_legacy_fixture(tmp_path):
    legacy_file = tmp_path / "mcp-connections.json"
    ConfigManager(legacy_file).set_mcp_configuration(["jira=stdio:jira"], None)

    store = MCPConnectionStore.from_workspace(SimpleNamespace(mcp_connections_file=legacy_file))

    assert store.global_file == legacy_file
    assert store.workspace_file == legacy_file
    assert [(entry.name, entry.scope, entry.active) for entry in store.entries()] == [
        ("jira", SCOPE_LOCAL, True)
    ]


def test_local_environment_overrides_global_without_local_spec(store):
    store.global_config.set_mcp_configuration(
        ["jira=stdio:jira"], ["jira=TOKEN=global,MODE=prod"]
    )
    store.workspace_config.set_mcp_configuration(None, ["jira=TOKEN=local"])
    store.workspace_config.set_mcp_active(["jira"])

    entry = store.entry("jira")

    assert entry.scope == SCOPE_GLOBAL
    assert entry.spec == "jira=stdio:jira"
    assert entry.env_spec == "jira=TOKEN=local"
    assert store.active_specs() == (["jira=stdio:jira"], ["jira=TOKEN=local"])


def test_client_toggle_persists_workspace_activation_and_restarts_only_when_enabled(
    store, monkeypatch
):
    store.global_config.set_mcp_configuration(["jira=stdio:jira"], None)
    store.workspace_config.set_mcp_active(["jira"])

    bridge = MCPClientBridge()
    bridge.mark_failed("jira", "offline", transport="stdio")
    set_bridge(bridge)
    executor = MagicMock(registry=ToolRegistry())
    manager = MCPConnectionManager(
        executor=executor,
        workspace=SimpleNamespace(),
        store=store,
    )
    started = []
    monkeypatch.setattr(
        manager,
        "_start_connection_thread",
        lambda spec, **_kwargs: started.append(spec),
    )

    try:
        disabled = manager.set_active("jira", False)
        assert not disabled.active
        assert store.workspace_config.mcp_active is None
        assert bridge.state("jira") is None

        enabled = manager.set_active("jira", True)
        assert enabled.active
        assert store.workspace_config.mcp_active == ["jira"]
        assert started == ["jira=stdio:jira"]
        assert bridge.state("jira").phase == MCPConnectionPhase.PENDING
    finally:
        set_bridge(None)


def _client_store(tmp_path):
    return MCPHttpSettingsStore(
        tmp_path / "global" / "mcp-server.json", tmp_path / "workspace" / "mcp-server.json"
    )


def test_http_client_local_activation_is_an_exception_keyed_by_name(tmp_path):
    """Sem padrão global, a liberação local liga só aqui; sem a chave nenhum client entra."""
    store = _client_store(tmp_path)
    assert store.client_decision("ChatGPT") == (False, SCOPE_GLOBAL)
    assert not store.is_client_active("ChatGPT")

    assert store.set_client_active("ChatGPT", True) == (True, SCOPE_LOCAL)
    assert store.set_client_active(" Claude Code (quimera) ", True) == (True, SCOPE_LOCAL)
    assert store.is_client_active("ChatGPT") and store.is_client_active("Claude Code (quimera)")
    assert not store.is_client_active("Grok")
    assert not store.global_file.exists()
    # A liberação convive com as demais chaves do overlay sem apagá-las.
    store.save("local", enabled=True, port=9096)
    assert store.layer("local")["enabled"] and store.load().port == 9096

    assert store.set_client_active("ChatGPT", False) == (False, SCOPE_LOCAL)
    assert json.loads(store.workspace_file.read_text(encoding="utf-8"))["http_clients_inactive"] == ["ChatGPT"]
    assert store.load().enabled
    with pytest.raises(ValueError, match="nome"):
        store.set_client_active("  ", True)
    with pytest.raises(ValueError, match="Escopo"):
        store.set_client_active("ChatGPT", True, "workspace")


def test_http_client_global_activation_is_the_default_and_local_exceptions_win(tmp_path):
    store = _client_store(tmp_path)
    other = MCPHttpSettingsStore(store.global_file, tmp_path / "other" / "mcp-server.json")

    # Em escopo global o padrão vale para todos os workspaces que não o contrariam.
    assert store.set_client_active("ChatGPT", True, SCOPE_GLOBAL) == (True, SCOPE_GLOBAL)
    assert other.client_decision("ChatGPT") == (True, SCOPE_GLOBAL)
    assert not store.workspace_file.exists() or "http_clients_active" not in store.workspace_file.read_text()

    # Uma exceção local bloqueia só este workspace; o padrão global segue intacto.
    assert store.set_client_active("ChatGPT", False, SCOPE_LOCAL) == (False, SCOPE_LOCAL)
    assert other.client_decision("ChatGPT") == (True, SCOPE_GLOBAL)
    assert json.loads(store.global_file.read_text(encoding="utf-8"))["http_clients_active"] == ["ChatGPT"]

    # Desligar o padrão não desfaz a exceção; ligar em escopo global faz este workspace voltar a segui-lo.
    other.set_client_active("ChatGPT", False, SCOPE_GLOBAL)
    assert store.client_decision("ChatGPT") == (False, SCOPE_LOCAL)
    assert store.set_client_active("ChatGPT", True, SCOPE_GLOBAL) == (True, SCOPE_GLOBAL)
    assert other.client_decision("ChatGPT") == (True, SCOPE_GLOBAL)
    assert "http_clients_inactive" not in json.loads(store.workspace_file.read_text(encoding="utf-8"))

    # Desativar em escopo global limpa também uma exceção ligada deste workspace.
    store.set_client_active("ChatGPT", True, SCOPE_LOCAL)
    assert store.set_client_active("ChatGPT", False, SCOPE_GLOBAL) == (False, SCOPE_GLOBAL)
    assert "ChatGPT" not in json.loads(store.global_file.read_text(encoding="utf-8")).get("http_clients_active", [])
    # O padrão global das conexões não vaza para o servidor HTTP: são arquivos distintos.
    assert store.load() == MCPHttpSettings()


def test_catalog_activation_scope_global_default_and_local_exceptions(store):
    store.global_config.set_mcp_configuration(["github=stdio:github", "jira=stdio:jira"], None)
    other = MCPConnectionStore(store.global_file, store.workspace_file.with_name("other.json"))

    # Padrão global: liga em todos os workspaces, sem tocar o overlay dos que o seguem.
    entry = store.set_active("github", True, SCOPE_GLOBAL)
    assert (entry.active, entry.activation_scope, entry.default_active) == (True, SCOPE_GLOBAL, True)
    assert json.loads(store.global_file.read_text(encoding="utf-8"))["mcp_active"] == ["github"]
    assert not store.workspace_file.exists() or store.workspace_config.mcp_active is None
    assert (other.entry("github").active, other.entry("github").activation_scope) == (True, SCOPE_GLOBAL)
    assert store.active_specs()[0] == ["github=stdio:github"]
    assert other.active_specs()[0] == ["github=stdio:github"]

    # Exceção local: desliga só aqui, e o padrão global não a desfaz.
    entry = store.set_active("github", False, SCOPE_LOCAL)
    assert (entry.active, entry.activation_scope, entry.default_active) == (False, SCOPE_LOCAL, True)
    assert store.workspace_config.mcp_inactive == ["github"]
    assert other.entry("github").active
    assert store.active_specs() == ([], [])

    # Exceção local ligada para o que o padrão deixa desligado.
    entry = store.set_active("jira", True, SCOPE_LOCAL)
    assert (entry.active, entry.activation_scope, entry.default_active) == (True, SCOPE_LOCAL, False)
    assert not other.entry("jira").active

    # Escopo global faz este workspace voltar a seguir o padrão, ligado ou desligado.
    entry = store.set_active("github", True, SCOPE_GLOBAL)
    assert (entry.active, entry.activation_scope) == (True, SCOPE_GLOBAL)
    assert store.workspace_config.mcp_inactive is None
    entry = store.set_active("jira", False, SCOPE_GLOBAL)
    assert (entry.active, entry.activation_scope) == (False, SCOPE_GLOBAL)
    assert store.workspace_config.mcp_active is None
    assert store.global_config.mcp_active == ["github"]

    with pytest.raises(ValueError, match="Escopo"):
        store.set_active("github", True, "workspace")


def test_local_connections_toggle_only_in_this_workspace(store):
    store.workspace_config.set_mcp_configuration(["ide=stdio:ide"], None)
    entry = store.entry("ide")
    assert (entry.active, entry.activation_scope) == (True, SCOPE_LOCAL)
    with pytest.raises(ValueError, match="padrão global"):
        store.set_active("ide", False, SCOPE_GLOBAL)

    entry = store.set_active("ide", False, SCOPE_LOCAL)
    assert (entry.active, entry.scope, entry.activation_scope) == (False, SCOPE_LOCAL, SCOPE_LOCAL)
    assert store.workspace_config.mcp_inactive == ["ide"] and store.workspace_config.mcp_active is None
    assert store.active_specs() == ([], [])

    # Reconfigurar a local desligada não a religa; --mcp-client (add_local) sim: pede a conexão nesta sessão.
    assert not store.upsert("ide=stdio:ide-v2").active
    store.add_local(["ide=stdio:ide-v2"], None)
    assert store.entry("ide").active and store.workspace_config.mcp_inactive is None

    # Remover a local apaga também a decisão, para não vazar a um nome futuro.
    store.set_active("ide", False, SCOPE_LOCAL)
    assert store.remove("ide") is None
    assert store.workspace_config.mcp_inactive is None
    assert not store.global_file.exists()


def test_normalize_keeps_an_identical_local_copy_switched_off(store):
    store.global_config.set_mcp_configuration(["github=stdio:github"], None)
    store.workspace_config.set_mcp_configuration(["github=stdio:github"], None)
    store.set_active("github", False, SCOPE_LOCAL)

    assert store.normalize() == ["github"]

    entry = store.entry("github")
    assert entry.is_global and not entry.active and entry.activation_scope == SCOPE_LOCAL
    assert store.workspace_config.mcp_clients is None
    assert store.workspace_config.mcp_active is None
    assert store.workspace_config.mcp_inactive == ["github"]


def test_saving_configuration_never_changes_activation(store):
    store.global_config.set_mcp_configuration(["jira=stdio:jira"], None)
    # Conexão global nova: entra no catálogo desligada em todos, inclusive aqui.
    entry = store.upsert("github=stdio:github", scope=SCOPE_GLOBAL)
    assert (entry.active, entry.activation_scope) == (False, SCOPE_GLOBAL)
    assert store.global_config.mcp_active is None and not store.workspace_file.exists()
    assert not store.entry("jira").active

    # Editar no global uma conexão desligada aqui por exceção: a exceção fica e o padrão não muda.
    store.set_active("github", True, SCOPE_GLOBAL)
    store.set_active("github", False, SCOPE_LOCAL)
    entry = store.upsert("github=stdio:github-v2", scope=SCOPE_GLOBAL)
    assert (entry.spec, entry.active, entry.activation_scope) == ("github=stdio:github-v2", False, SCOPE_LOCAL)
    assert store.workspace_config.mcp_inactive == ["github"]
    assert store.global_config.mcp_active == ["github"]

    # Editar no local uma global desligada aqui vira override local, ainda desligado.
    entry = store.upsert("github=stdio:github-local", scope=SCOPE_LOCAL)
    assert (entry.scope, entry.active, entry.activation_scope, entry.default_active) == (
        SCOPE_LOCAL, False, SCOPE_LOCAL, True,
    )
    assert store.active_specs() == ([], [])

    # Ligar o override é decisão só daqui; uma local não tem padrão global.
    entry = store.set_active("github", True, SCOPE_LOCAL)
    assert entry.active and store.workspace_config.mcp_inactive is None
    with pytest.raises(ValueError, match="padrão global"):
        store.set_active("github", True, SCOPE_GLOBAL)

    # Devolver o override ao catálogo mantém o estado: o padrão já o liga, então sem exceção.
    entry = store.upsert("github=stdio:github-local", scope=SCOPE_GLOBAL)
    assert (entry.is_global, entry.active, entry.activation_scope) == (True, True, SCOPE_GLOBAL)
    assert store.workspace_config.mcp_clients is None and store.workspace_config.mcp_active is None

    # Editar no global uma ligada só aqui (padrão desligado) mantém a exceção ligada.
    store.set_active("github", False, SCOPE_GLOBAL)
    store.set_active("github", True, SCOPE_LOCAL)
    entry = store.upsert("github=stdio:github-v3", scope=SCOPE_GLOBAL)
    assert (entry.active, entry.activation_scope, entry.default_active) == (True, SCOPE_LOCAL, False)
    assert store.workspace_config.mcp_active == ["github"] and store.global_config.mcp_active is None

    # Remover do catálogo tira também o padrão e as exceções, para o nome não vazar.
    store.set_active("github", False, SCOPE_LOCAL)
    assert store.remove("github") is None
    assert store.global_config.mcp_active is None
    assert store.workspace_config.mcp_inactive is None and store.workspace_config.mcp_active is None


def test_single_file_legacy_http_store_treats_global_client_activation_as_local(tmp_path):
    """Com um só arquivo não há padrão a separar da exceção: a liberação fica gravada."""
    path = tmp_path / "mcp-server.json"
    store = MCPHttpSettingsStore(path, path)
    assert store.set_client_active("ChatGPT", True, SCOPE_GLOBAL) == (True, SCOPE_LOCAL)
    assert json.loads(path.read_text(encoding="utf-8")) == {"http_clients_active": ["ChatGPT"]}
    assert store.set_client_active("ChatGPT", False, SCOPE_GLOBAL) == (False, SCOPE_LOCAL)
    assert store.is_client_active("ChatGPT") is False
