"""Configuração persistida do MCP em dois escopos: global e workspace.

Cada parte do MCP tem um par de arquivos com o mesmo nome, um em
``<base_dir>`` e outro no diretório do workspace:

* ``mcp-connections.json`` — servidores MCP externos (conexões cliente). O
  global é o catálogo: specs e ambiente de cada conexão, compartilhados por
  todos os workspaces, e ``mcp_active`` lista as ligadas em todos eles. O do
  workspace é um overlay: ``mcp_active`` liga aqui uma conexão que o padrão
  global deixa desligada, ``mcp_inactive`` desliga aqui uma que ele liga;
  ``mcp_clients``/``mcp_client_env`` guardam conexões locais, que valem só
  neste workspace e estão sempre ativas. Uma local com o nome de uma global a
  sobrepõe (ex.: a porta do IDE desta janela).
* ``mcp-server.json`` — servidor MCP HTTP do Quimera. O global guarda os
  padrões (host, porta, allowlist); o do workspace sobrepõe chave a chave. Ligar
  o servidor (``http_enabled``) é sempre decisão do workspace, nunca herdada:
  todo Quimera aberto disputaria a mesma porta. Os clients OAuth liberados a
  usar o servidor seguem a mesma regra das conexões: ``http_clients_active``
  global é o padrão de todos os workspaces, e o overlay liga
  (``http_clients_active``) ou bloqueia (``http_clients_inactive``) aqui.

Em ambos os casos a decisão tem escopo. Ativar ou desativar em escopo global
muda o padrão e faz este workspace voltar a segui-lo; em escopo local grava uma
exceção que vale só aqui e que o padrão global não desfaz.

O estado OAuth do servidor (clients e tokens) segue em
``<base_dir>/state/mcp-server.json``; é estado, não configuração. O cadastro
é global, como o catálogo de conexões; o que tem escopo é a liberação.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from quimera.config import ConfigManager
from quimera.config_store import read_json_object, update_json_object
from quimera.runtime.mcp.client import _spec_name, merge_specs_by_name

SCOPE_GLOBAL = "global"
SCOPE_LOCAL = "local"
MCP_SCOPES = (SCOPE_GLOBAL, SCOPE_LOCAL)

DEFAULT_MCP_HTTP_HOST = "127.0.0.1"
DEFAULT_MCP_HTTP_PORT = 9090
DEFAULT_MCP_HTTP_ALLOW_TOOLS = "read"


def _check_scope(scope: str) -> None:
    if scope not in MCP_SCOPES:
        raise ValueError(f"Escopo MCP inválido: {scope!r}; esperado global ou local")


def _by_name(specs: list[str] | None) -> dict[str, str]:
    """Indexa specs ``nome=...`` pelo nome, preservando a ordem do arquivo."""
    indexed: dict[str, str] = {}
    for spec in specs or []:
        name = _spec_name(spec)
        if name:
            indexed[name] = spec
    return indexed


def _without(specs: list[str] | None, name: str) -> list[str]:
    return [spec for spec in (specs or []) if _spec_name(spec) != name]


def _specs(data: dict, key: str) -> list[str]:
    value = data.get(key)
    return value if isinstance(value, list) and all(isinstance(item, str) for item in value) else []


def _set_list(data: dict, key: str, values: list[str]) -> None:
    if values:
        data[key] = values
    else:
        data.pop(key, None)


def _with(names: list[str], name: str, present: bool) -> list[str]:
    """``names`` com ``name`` ao fim, se ``present`` e ainda não estiver; senão sem ele."""
    if present:
        return names if name in names else [*names, name]
    return [item for item in names if item != name]


def _resolve_activation(default: bool, pinned_on: bool, pinned_off: bool) -> tuple[bool, str]:
    """``(ativo, quem decide)``: a exceção do workspace vence; sem ela, vale o padrão global."""
    if pinned_off:
        return False, SCOPE_LOCAL
    if pinned_on:
        return True, SCOPE_LOCAL
    return default, SCOPE_GLOBAL


def env_pairs(env_spec: str) -> dict[str, str]:
    """Pares ``KEY=valor`` de uma spec ``nome=KEY=valor,KEY2=valor2``."""
    rest = env_spec.split("=", 1)[1] if "=" in env_spec else ""
    pairs: dict[str, str] = {}
    for part in rest.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip():
            pairs[key.strip()] = value.strip()
    return pairs


@dataclass(frozen=True)
class MCPConnectionEntry:
    """Uma conexão cliente resolvida para o workspace atual.

    ``scope`` diz onde a spec em vigor está gravada; ``active`` diz se ela
    entra nesta sessão. Locais são sempre ativas; as globais seguem o padrão
    global, a menos que este workspace tenha uma exceção. ``activation_scope``
    diz quem decidiu ``active`` (``global``: o padrão; ``local``: este
    workspace) e ``default_active`` guarda o padrão global para explicá-lo.
    """

    name: str
    spec: str
    env_spec: str = ""
    scope: str = SCOPE_LOCAL
    active: bool = True
    activation_scope: str = SCOPE_LOCAL
    default_active: bool = False

    @property
    def is_global(self) -> bool:
        return self.scope == SCOPE_GLOBAL

    @property
    def env_text(self) -> str:
        """``KEY=valor,...`` sem o nome da conexão (vazio se não há ambiente)."""
        return self.env_spec.split("=", 1)[1].strip() if "=" in self.env_spec else ""


class MCPConnectionStore:
    """Catálogo global de conexões cliente com ativação e overrides por workspace."""

    def __init__(self, global_file: Path, workspace_file: Path) -> None:
        self.global_file = Path(global_file)
        self.workspace_file = Path(workspace_file)

    @classmethod
    def from_workspace(cls, workspace) -> "MCPConnectionStore":
        """Cria o store aceitando tanto o contrato novo quanto fixtures legadas.

        Workspaces atuais expõem dois caminhos: o catálogo global e o overlay
        local. Fixtures e instalações anteriores só conhecem
        ``mcp_connections_file``; nesse caso o arquivo único continua sendo
        lido sem exigir uma migração destrutiva ou edição manual.
        """
        workspace_file = getattr(workspace, "mcp_connections_file", None)
        if workspace_file is None:
            raise AttributeError("workspace precisa expor mcp_connections_file")
        global_file = getattr(workspace, "global_mcp_connections_file", None) or workspace_file
        return cls(global_file, workspace_file)

    @property
    def global_config(self) -> ConfigManager:
        return ConfigManager(self.global_file)

    @property
    def workspace_config(self) -> ConfigManager:
        return ConfigManager(self.workspace_file)

    def _config(self, scope: str) -> ConfigManager:
        _check_scope(scope)
        return self.global_config if scope == SCOPE_GLOBAL else self.workspace_config

    # ── leitura ──────────────────────────────────────────────────────

    def entries(self) -> list[MCPConnectionEntry]:
        """Conexões visíveis neste workspace: catálogo (na ordem dele) e depois as só-locais.

        Spec e ambiente são resolvidos pelo nome, cada um de forma
        independente, e o workspace vence: um override local sem ambiente
        próprio herda o ambiente do catálogo.
        """
        # Cada arquivo é lido uma só vez: spec e ambiente devem vir da mesma
        # revisão, inclusive enquanto outro workspace edita o catálogo.
        overlay = read_json_object(self.workspace_file)
        local_specs = _by_name(_specs(overlay, "mcp_clients"))
        local_env = _by_name(_specs(overlay, "mcp_client_env"))
        if self.global_file == self.workspace_file:
            return [
                MCPConnectionEntry(name, spec, local_env.get(name, ""))
                for name, spec in local_specs.items()
            ]
        catalog = read_json_object(self.global_file)
        catalog_specs = _by_name(_specs(catalog, "mcp_clients"))
        catalog_env = _by_name(_specs(catalog, "mcp_client_env"))
        default_on = set(_specs(catalog, "mcp_active"))
        pinned_on = set(_specs(overlay, "mcp_active"))
        pinned_off = set(_specs(overlay, "mcp_inactive"))

        entries: list[MCPConnectionEntry] = []
        for name, spec in catalog_specs.items():
            if name in local_specs:
                entries.append(
                    MCPConnectionEntry(
                        name,
                        local_specs[name],
                        local_env.get(name, catalog_env.get(name, "")),
                        SCOPE_LOCAL,
                        True,
                        SCOPE_LOCAL,
                        name in default_on,
                    )
                )
            else:
                active, decided_by = _resolve_activation(
                    name in default_on, name in pinned_on, name in pinned_off
                )
                entries.append(
                    MCPConnectionEntry(
                        name,
                        spec,
                        # O ambiente é um overlay independente da spec: um
                        # workspace pode trocar apenas TOKEN sem duplicar a
                        # conexão global em ``mcp_clients``.
                        local_env.get(name, catalog_env.get(name, "")),
                        SCOPE_GLOBAL,
                        active,
                        decided_by,
                        name in default_on,
                    )
                )
        for name, spec in local_specs.items():
            if name not in catalog_specs:
                entries.append(
                    MCPConnectionEntry(name, spec, local_env.get(name, ""), SCOPE_LOCAL, True)
                )
        return entries

    def entry(self, name: str) -> MCPConnectionEntry | None:
        return next((item for item in self.entries() if item.name == name), None)

    def active_specs(self) -> tuple[list[str], list[str]]:
        """``(specs, env_specs)`` das conexões que entram nesta sessão."""
        active = [item for item in self.entries() if item.active]
        return (
            [item.spec for item in active],
            [item.env_spec for item in active if item.env_spec],
        )

    # ── escrita ──────────────────────────────────────────────────────

    def set_active(
        self, name: str, active: bool, scope: str = SCOPE_LOCAL
    ) -> MCPConnectionEntry:
        """Liga ou desliga uma conexão do catálogo e devolve a entrada resolvida.

        Em escopo local grava uma exceção deste workspace, que o padrão global
        não desfaz. Em escopo global muda o padrão de todos os workspaces e
        este volta a segui-lo; os demais, já abertos, só o aplicam na próxima
        partida.
        """
        _check_scope(scope)
        entry = self.entry(name)
        if entry is None:
            raise KeyError(f"Conexão MCP não configurada: {name}")
        if not entry.is_global:
            raise ValueError(
                f"A conexão '{name}' é local a este workspace e está sempre ativa; "
                "remova-a para desligá-la."
            )
        if scope == SCOPE_GLOBAL:
            update_json_object(
                self.global_file,
                lambda data: _set_list(
                    data, "mcp_active", _with(_specs(data, "mcp_active"), name, active)
                ),
            )
            self._decide_here(name, None)
        else:
            self._decide_here(name, active)
        return self.entry(name)

    def _decide_here(self, name: str, active: bool | None, *, drop_local: bool = False) -> None:
        """Grava a decisão do workspace: exceção ligada, exceção desligada ou, com ``None``, seguir o padrão.

        Seguir o padrão só apaga exceções; sem nenhuma a apagar, o overlay nem
        é criado.
        """
        keys = ("mcp_active", "mcp_inactive") + (("mcp_clients", "mcp_client_env") if drop_local else ())
        if active is None:
            snapshot = read_json_object(self.workspace_file)
            if all(name not in _by_name(_specs(snapshot, key)) for key in keys):
                return

        def update(data):
            if drop_local:
                for key in ("mcp_clients", "mcp_client_env"):
                    _set_list(data, key, _without(_specs(data, key), name))
            _set_list(data, "mcp_active", _with(_specs(data, "mcp_active"), name, active is True))
            _set_list(data, "mcp_inactive", _with(_specs(data, "mcp_inactive"), name, active is False))

        update_json_object(self.workspace_file, update)

    def upsert(
        self,
        spec: str,
        *,
        env_spec: str | None = None,
        scope: str = SCOPE_LOCAL,
    ) -> MCPConnectionEntry:
        """Grava a conexão no escopo pedido e devolve a entrada resolvida.

        ``env_spec`` ``None`` preserva o ambiente já gravado naquele escopo;
        vazio o remove. Escopo global quer dizer "em todos os workspaces": a
        spec entra no catálogo e no padrão de ativação, como Ativar em escopo
        global, e este workspace larga a exceção que tinha e volta a seguir o
        padrão; um override local de mesmo nome deixa de existir (senão a
        edição ficaria invisível). No escopo local, uma cópia idêntica ao
        catálogo não é gravada: a conexão global é apenas ligada aqui; e um
        ambiente local igual ao do catálogo não é gravado, fica herdado.
        """
        name = _spec_name(spec)
        if not name:
            raise ValueError("Conexão MCP exige um nome")
        _check_scope(scope)
        if self.global_file == self.workspace_file:
            self._write(self.workspace_file, name, spec, env_spec)
            return self.entry(name)
        if scope == SCOPE_GLOBAL:
            existing = self.entry(name)
            if env_spec is None and existing is not None:
                env_spec = existing.env_spec
            self._write(self.global_file, name, spec, env_spec, activate=True)
            self._decide_here(name, None, drop_local=True)
            return self.entry(name)

        catalog_specs = _by_name(self.global_config.mcp_clients)
        catalog_env = env_pairs(_by_name(self.global_config.mcp_client_env).get(name, ""))
        if env_spec is None:
            local_env = _by_name(self.workspace_config.mcp_client_env).get(name)
            local_pairs = env_pairs(local_env) if local_env is not None else catalog_env
        else:
            local_pairs = env_pairs(env_spec)
        inherits_env = name in catalog_specs and local_pairs == catalog_env
        if catalog_specs.get(name) == spec and inherits_env:
            self._decide_here(name, True, drop_local=True)
            return self.entry(name)
        if env_spec is not None and not local_pairs and catalog_env:
            # Uma entrada vazia explícita bloqueia a herança de credenciais.
            env_spec = f"{name}="
        self._write(self.workspace_file, name, spec, "" if inherits_env else env_spec)
        return self.entry(name)

    def remove(self, name: str) -> MCPConnectionEntry | None:
        """Remove a conexão do escopo em que vive.

        Uma global sai do catálogo, do padrão de ativas e das exceções deste
        workspace. Uma local sai do workspace; se o catálogo tiver uma de mesmo
        nome, ela volta a valer e é devolvida (ativa ou não, conforme a
        decisão em vigor).
        """
        entry = self.entry(name)
        if entry is None:
            raise KeyError(f"Conexão MCP não configurada: {name}")
        if entry.is_global:
            self._drop(self.global_file, name, ("mcp_clients", "mcp_client_env", "mcp_active"))
            # Inclui overrides só de ambiente, que não devem vazar para uma
            # conexão futura que reutilize o nome removido.
            self._decide_here(name, None, drop_local=True)
            return None
        self._drop(self.workspace_file, name)
        return self.entry(name)

    def add_local(self, specs: list[str] | None, env_specs: list[str] | None) -> None:
        """Mescla specs da linha de comando como conexões deste workspace.

        Mesmo precedente de ``--mcp-client``: a spec fica persistida. O
        ambiente de uma conexão não citada em ``env_specs`` é preservado.
        """
        env_by_name = _by_name(env_specs)
        for spec in specs or []:
            self.upsert(spec, env_spec=env_by_name.get(_spec_name(spec)), scope=SCOPE_LOCAL)

    def normalize(self) -> list[str]:
        """Converte cópias locais idênticas ao catálogo em ativações.

        O catálogo global nasceu da fusão dos arquivos por workspace, que
        continuam com as mesmas specs. Uma local idêntica à global é a mesma
        conexão e, mantida como cópia, deixaria de acompanhar o catálogo.
        Devolve os nomes convertidos; sem duplicatas, nada é gravado.
        """
        if self.global_file == self.workspace_file:
            return []
        catalog = read_json_object(self.global_file)
        catalog_specs = _by_name(_specs(catalog, "mcp_clients"))
        catalog_env = _by_name(_specs(catalog, "mcp_client_env"))
        absorbed: list[str] = []
        if not catalog_specs:
            return []

        def candidates(data):
            local_env = _by_name(_specs(data, "mcp_client_env"))
            names = []
            for spec in _specs(data, "mcp_clients"):
                name = _spec_name(spec)
                if catalog_specs.get(name) != spec:
                    continue
                if name in local_env and env_pairs(local_env[name]) != env_pairs(catalog_env.get(name, "")):
                    continue
                names.append(name)
            return names

        def update(data):
            absorbed.extend(candidates(data))
            for key in ("mcp_clients", "mcp_client_env"):
                _set_list(data, key, [item for item in _specs(data, key) if _spec_name(item) not in absorbed])
            active = _specs(data, "mcp_active")
            _set_list(data, "mcp_active", active + [name for name in absorbed if name not in active])
            _set_list(data, "mcp_inactive", [item for item in _specs(data, "mcp_inactive") if item not in absorbed])

        # Evita criar um overlay quando o workspace ainda não tem conexões.
        if not candidates(read_json_object(self.workspace_file)):
            return []
        update_json_object(self.workspace_file, update)
        return absorbed

    @staticmethod
    def _write(path: Path, name: str, spec: str, env_spec: str | None, *, activate: bool = False) -> None:
        def update(data):
            data["mcp_clients"] = merge_specs_by_name(_specs(data, "mcp_clients"), [spec])
            if env_spec is not None:
                env_specs = _specs(data, "mcp_client_env")
                env_specs = merge_specs_by_name(env_specs, [env_spec]) if env_spec.strip() else _without(env_specs, name)
                _set_list(data, "mcp_client_env", env_specs)
            if activate:
                _set_list(data, "mcp_active", _with(_specs(data, "mcp_active"), name, True))

        update_json_object(path, update)

    @staticmethod
    def _drop(path: Path, name: str, keys: tuple[str, ...] = ("mcp_clients", "mcp_client_env")) -> None:
        snapshot = read_json_object(path)
        if all(name not in _by_name(_specs(snapshot, key)) for key in keys):
            return

        def update(data):
            for key in keys:
                _set_list(data, key, _without(_specs(data, key), name))

        update_json_object(path, update)


@dataclass(frozen=True)
class MCPHttpSettings:
    """Configuração efetiva do servidor MCP HTTP para um workspace."""

    enabled: bool = False
    host: str = DEFAULT_MCP_HTTP_HOST
    port: int = DEFAULT_MCP_HTTP_PORT
    allow_tools: str = DEFAULT_MCP_HTTP_ALLOW_TOOLS

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"


def validate_http_settings(**values) -> dict:
    """Valida valores parciais de :class:`MCPHttpSettings`; ``None`` passa intacto."""
    checked: dict = {}
    for key, value in values.items():
        if key not in MCPHttpSettings.__dataclass_fields__:
            raise ValueError(f"Chave desconhecida do servidor MCP HTTP: {key}")
        if value is None:
            checked[key] = None
        elif key == "enabled":
            if not isinstance(value, bool):
                raise ValueError("Ativação do servidor MCP HTTP deve ser booleana.")
            checked[key] = value
        elif key == "port":
            if type(value) is not int or not 0 < value < 65536:
                raise ValueError("Porta do servidor MCP HTTP deve ser um inteiro entre 1 e 65535.")
            checked[key] = value
        else:
            text = str(value).strip()
            if not text:
                raise ValueError(f"Informe um valor para {key}.")
            checked[key] = text
    return checked


class MCPHttpSettingsStore:
    """Padrões globais do servidor MCP HTTP sobrepostos pelo workspace, chave a chave."""

    def __init__(self, global_file: Path, workspace_file: Path) -> None:
        self.global_file = Path(global_file)
        self.workspace_file = Path(workspace_file)

    @classmethod
    def from_workspace(cls, workspace) -> "MCPHttpSettingsStore":
        """Cria o store com os caminhos novos ou com um workspace legado.

        A UI também é usada por fixtures mínimas que só fornecem o arquivo de
        conexões. Derivar ``mcp-server.json`` ao lado dele mantém esse contrato
        antigo isolado e não mistura configuração HTTP com o JSON principal.
        """
        connections_file = getattr(workspace, "mcp_connections_file", None)
        workspace_file = getattr(workspace, "mcp_server_settings_file", None)
        if workspace_file is None and connections_file is not None:
            workspace_file = Path(connections_file).with_name("mcp-server.json")
        if workspace_file is None:
            raise AttributeError("workspace precisa expor mcp_server_settings_file")
        global_file = getattr(workspace, "global_mcp_server_settings_file", None) or workspace_file
        return cls(global_file, workspace_file)

    def _config(self, scope: str) -> ConfigManager:
        _check_scope(scope)
        return ConfigManager(self.global_file if scope == SCOPE_GLOBAL else self.workspace_file)

    def layer(self, scope: str) -> dict:
        """Chaves gravadas naquele escopo, sem herança."""
        return self._config(scope).mcp_http_settings

    def _inherited(self, scope: str) -> dict:
        """Chaves que um escopo contribui à configuração efetiva.

        ``enabled`` só vale no workspace: um ``http_enabled`` no arquivo global
        é ignorado, senão todo Quimera sem override subiria o servidor e
        disputaria a mesma porta.
        """
        layer = self.layer(scope)
        if scope == SCOPE_GLOBAL:
            layer.pop("enabled", None)
        return layer

    def load(self) -> MCPHttpSettings:
        """Configuração efetiva: padrões ← global ← workspace."""
        return MCPHttpSettings(**{**self._inherited(SCOPE_GLOBAL), **self._inherited(SCOPE_LOCAL)})

    def sources(self) -> dict[str, str]:
        """Escopo de origem de cada chave efetiva: ``global``, ``local`` ou ``default``."""
        origin = {key: "default" for key in MCPHttpSettings.__dataclass_fields__}
        for scope in (SCOPE_GLOBAL, SCOPE_LOCAL):
            for key in self._inherited(scope):
                origin[key] = scope
        return origin

    def save(self, scope: str, **values) -> MCPHttpSettings:
        """Atualiza as chaves informadas naquele escopo (``None`` apaga) e devolve o efetivo."""
        config = self._config(scope)
        validated = validate_http_settings(**values)
        if scope == SCOPE_GLOBAL and validated.get("enabled") is not None:
            raise ValueError("Ligar o servidor MCP HTTP é decisão de cada workspace, não global.")
        config.set_mcp_http_settings(**validated)
        return self.load()

    # ── clients OAuth liberados a usar o servidor ────────────────────

    def client_decision(self, name: str) -> tuple[bool, str]:
        """``(liberado, quem decide)`` para o client de nome ``name`` neste workspace.

        O cadastro OAuth (clients e tokens) é global, mas a liberação tem
        escopo, como ``mcp_active`` nas conexões: ``http_clients_active`` do
        arquivo global é o padrão de todos os workspaces, e o overlay o
        contraria com uma exceção, ligando (``http_clients_active``) ou
        bloqueando (``http_clients_inactive``) o client aqui. Sem nenhuma das
        duas, nenhum client entra. A identidade é o nome que o client declara
        ao se registrar, porque ChatGPT e similares recebem um ``client_id``
        novo a cada registro dinâmico, e o nome é o que o usuário reconhece na
        tela de consentimento e no Hub.
        """
        name = str(name or "").strip()
        overlay = read_json_object(self.workspace_file)
        return _resolve_activation(
            name in _specs(read_json_object(self.global_file), "http_clients_active"),
            name in _specs(overlay, "http_clients_active"),
            name in _specs(overlay, "http_clients_inactive"),
        )

    def is_client_active(self, name: str) -> bool:
        """``True`` se o client de nome ``name`` está liberado neste workspace."""
        return self.client_decision(name)[0]

    def set_client_active(self, name: str, active: bool, scope: str = SCOPE_LOCAL) -> tuple[bool, str]:
        """Libera ou bloqueia o client e devolve ``client_decision`` resultante.

        Em escopo local grava a exceção deste workspace. Em escopo global muda
        o padrão de todos os workspaces e este volta a segui-lo.
        """
        _check_scope(scope)
        name = str(name or "").strip()
        if not name:
            raise ValueError("Client OAuth exige um nome")
        if self.global_file == self.workspace_file:
            # Fixture legada de arquivo único: só existe a camada do workspace.
            # Gravar o padrão e em seguida "apagar a exceção" no mesmo arquivo
            # desfaria a própria gravação.
            scope = SCOPE_LOCAL

        if scope == SCOPE_GLOBAL:
            update_json_object(
                self.global_file,
                lambda data: _set_list(
                    data, "http_clients_active", _with(_specs(data, "http_clients_active"), name, active)
                ),
            )

        def update_here(data):
            pin = None if scope == SCOPE_GLOBAL else active
            _set_list(data, "http_clients_active", _with(_specs(data, "http_clients_active"), name, pin is True))
            _set_list(data, "http_clients_inactive", _with(_specs(data, "http_clients_inactive"), name, pin is False))

        # Em escopo global o workspace só volta a seguir o padrão: sem exceção
        # a apagar, o overlay nem é criado.
        overlay = read_json_object(self.workspace_file)
        if scope == SCOPE_LOCAL or any(
            name in _specs(overlay, key) for key in ("http_clients_active", "http_clients_inactive")
        ):
            update_json_object(self.workspace_file, update_here)
        return self.client_decision(name)
