from quimera.environment import (
    RuntimeSecrets,
    build_env_vars,
    load_workspace_environment,
)
from quimera.workspace import Workspace


def _workspace(monkeypatch, project, base_dir):
    base_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("quimera.workspace.find_base_writable", lambda _dirs: base_dir)
    return Workspace(project)


def test_runtime_secrets_prefers_process_then_canonical_then_legacy(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("KEY=legacy\nLEGACY_ONLY=yes\n", encoding="utf-8")
    (tmp_path / "secrets.env").write_text("KEY=canonical\n", encoding="utf-8")

    workspace = _workspace(monkeypatch, tmp_path / "project", tmp_path)
    secrets = RuntimeSecrets(workspace, environ={})
    assert secrets.get("KEY") == "canonical"
    assert secrets.get("LEGACY_ONLY") == "yes"

    process_secrets = RuntimeSecrets(workspace, environ={"KEY": "process"})
    assert process_secrets.get("KEY") == "process"


def test_workspace_environment_is_project_local_and_does_not_create_files(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    workspace = _workspace(monkeypatch, project, tmp_path / "runtime")

    assert load_workspace_environment(workspace) == {}
    assert not (project / ".quimera").exists()

    config_dir = project / ".quimera"
    config_dir.mkdir()
    (config_dir / ".env").write_text("AWS_PROFILE=precocerto\n", encoding="utf-8")

    assert load_workspace_environment(workspace) == {"AWS_PROFILE": "precocerto"}


def test_agent_environment_hides_global_file_secrets_and_applies_workspace_env(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "secrets.env").write_text(
        "OPENAI_API_KEY=private\nSHARED=runtime\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project_config = project / ".quimera"
    project_config.mkdir(parents=True)
    (project_config / ".env").write_text(
        "AWS_PROFILE=precocerto\nSHARED=workspace\n",
        encoding="utf-8",
    )

    workspace = _workspace(monkeypatch, project, runtime_dir)
    env = build_env_vars(
        {"PATH": "/usr/bin", "OPENAI_API_KEY": "leaked-parent", "SHARED": "leaked-parent"},
        workspace=workspace,
        runtime_secrets=RuntimeSecrets(workspace, environ={}),
    )

    assert "OPENAI_API_KEY" not in env
    assert env["AWS_PROFILE"] == "precocerto"
    assert env["SHARED"] == "workspace"
    assert env["PATH"] == "/usr/bin"


def test_runtime_environment_hides_private_and_provider_keys(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "secrets.env").write_text(
        "FILE_SECRET=private\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "quimera.environment.configured_provider_secret_keys",
        lambda: {"PROVIDER_SECRET"},
    )

    workspace = _workspace(monkeypatch, tmp_path / "project", runtime_dir)
    env = build_env_vars(
        {
            "PATH": "/usr/bin",
            "FILE_SECRET": "leaked-file",
            "PROVIDER_SECRET": "leaked-provider",
            "SAFE": "yes",
        },
        workspace=workspace,
        runtime_secrets=RuntimeSecrets(workspace, environ={}),
        extra_env={"EXPLICIT_TOKEN": "allowed"},
    )

    assert "FILE_SECRET" not in env
    assert "PROVIDER_SECRET" not in env
    assert env["SAFE"] == "yes"
    assert env["EXPLICIT_TOKEN"] == "allowed"


def test_runtime_secrets_can_restore_legacy_process_configuration_without_overwriting_explicit_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "QUIMERA_LOG_LEVEL=DEBUG\nOPENAI_API_KEY=legacy-key\n",
        encoding="utf-8",
    )
    (tmp_path / "secrets.env").write_text(
        "OPENAI_API_KEY=canonical-key\nQUIMERA_MCP_TOKEN=private-token\n",
        encoding="utf-8",
    )
    environ = {"QUIMERA_LOG_LEVEL": "INFO"}

    workspace = _workspace(monkeypatch, tmp_path / "project", tmp_path)
    RuntimeSecrets(workspace, environ=environ).apply_to_environ(environ)

    assert environ["QUIMERA_LOG_LEVEL"] == "INFO"
    assert environ["OPENAI_API_KEY"] == "canonical-key"
    assert environ["QUIMERA_MCP_TOKEN"] == "private-token"


def test_workspace_protects_runtime_credentials_without_hiding_workspace_history(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SECRET=legacy\n", encoding="utf-8")
    (tmp_path / "secrets.env").write_text("SECRET=canonical\n", encoding="utf-8")
    (tmp_path / "connections.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "connections.json.bak").write_text("{}\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "mcp_oauth.json").write_text("{}\n", encoding="utf-8")
    project = tmp_path / "project"
    quimera_workspace = _workspace(monkeypatch, project, tmp_path)
    workspace = quimera_workspace.root
    quimera_workspace.mcp_config_file.write_text("{}\n", encoding="utf-8")
    sibling_workspace = tmp_path / "workspaces" / "def"
    sibling_workspace.mkdir(parents=True)
    sibling_config = sibling_workspace / "config.json"
    sibling_config.write_text("{}\n", encoding="utf-8")
    history = workspace / "data" / "history.jsonl"
    history.parent.mkdir(exist_ok=True)
    history.write_text("history\n", encoding="utf-8")

    protected = set(quimera_workspace.protected_files)

    assert (tmp_path / ".env").resolve() in protected
    assert (tmp_path / "secrets.env").resolve() in protected
    assert (tmp_path / "connections.json").resolve() in protected
    assert (tmp_path / "connections.json.bak").resolve() in protected
    assert (state / "mcp_oauth.json").resolve() in protected
    assert (workspace / "config.json").resolve() in protected
    assert sibling_config.resolve() not in protected
    assert history.resolve() not in protected


def test_configured_provider_key_is_private_even_when_only_exported_by_parent(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    workspace = _workspace(monkeypatch, project, runtime_dir)
    monkeypatch.setattr(
        "quimera.environment.configured_provider_secret_keys",
        lambda: {"PROVIDER_API_KEY"},
    )

    env = build_env_vars(
        {"PATH": "/usr/bin", "PROVIDER_API_KEY": "parent-secret"},
        workspace=workspace,
        runtime_secrets=RuntimeSecrets(workspace, environ={}),
    )

    assert "PROVIDER_API_KEY" not in env


def test_workspace_env_can_explicitly_reexpose_configured_provider_key(tmp_path, monkeypatch):
    project = tmp_path / "project"
    config_dir = project / ".quimera"
    config_dir.mkdir(parents=True)
    (config_dir / ".env").write_text("PROVIDER_API_KEY=workspace-value\n", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    workspace = _workspace(monkeypatch, project, runtime_dir)
    monkeypatch.setattr(
        "quimera.environment.configured_provider_secret_keys",
        lambda: {"PROVIDER_API_KEY"},
    )

    env = build_env_vars(
        {"PROVIDER_API_KEY": "parent-secret"},
        workspace=workspace,
        runtime_secrets=RuntimeSecrets(workspace, environ={}),
    )

    assert env["PROVIDER_API_KEY"] == "workspace-value"
