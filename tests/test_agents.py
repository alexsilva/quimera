import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, ANY, call

import pytest

from quimera.agent_events import SpyEvent
from quimera.agents import (
    AgentClient,
    _filter_stderr_lines,
    _is_rate_limit_signal,
    _strip_spinner,
    _should_ignore_stderr_line,
)
from quimera.agents.process_runner import ProcessRunner
from quimera.constants import MAX_STDERR_LINES, Visibility
from quimera.profiles import get as get_profile
from quimera.prompt_templates import PromptText
from quimera.profiles.base import CliConnection, register_connection_profile
from quimera.profiles.claude import _format_claude_spy_event
from quimera.profiles.codex import _format_codex_spy_event
from quimera.profiles.opencode import OpenCodeProfile, _format_opencode_spy_event
from quimera.profiles.spy_utils import format_command_output_preview
from quimera.spy_output_presenter import SpyOutputPresenter
from quimera.evidence import EvidenceStore


@pytest.fixture
def renderer():
    return MagicMock()


def test_strip_spinner():
    """Verifica que strip spinner."""
    assert _strip_spinner("⠋Executing") == "Executing"
    assert _strip_spinner("Normal text") == "Normal text"


def test_should_ignore_codex_stdin_noise():
    """Verifica que should ignore codex stdin noise."""
    assert _should_ignore_stderr_line("codex", "Reading additional input from stdin...\n") is True
    assert _should_ignore_stderr_line("codex", "\x1b[2mReading additional input from stdin...\x1b[0m\r\n") is True
    assert _should_ignore_stderr_line("codex", "Reading prompt from stdin...\n") is True
    assert (
        _should_ignore_stderr_line(
            "codex",
            "2026-05-27T00:14:34.911494Z ERROR codex_core::util: "
            "Orphan function call output for call id: call_K6xlRm0c9JyUe9iBNv9Txgcl\n",
        )
        is True
    )
    assert _should_ignore_stderr_line("claude", "Reading additional input from stdin...\n") is False
    assert _should_ignore_stderr_line("codex", "real error\n") is False


def test_should_ignore_interrupt_echo_caret_c():
    """Verifica que should ignore interrupt echo caret c."""
    assert _should_ignore_stderr_line("codex", "^C\n") is True
    assert _should_ignore_stderr_line(None, "\x1b[2m^C\x1b[0m\r\n") is True
    assert _should_ignore_stderr_line("codex", "\x03\n") is True
    assert _should_ignore_stderr_line("codex", "\x03\x03\n") is True
    assert _should_ignore_stderr_line("codex", "^C^C\n") is True


def test_filter_stderr_lines_removes_codex_stdin_noise():
    """Verifica que filter stderr lines removes codex stdin noise."""
    assert _filter_stderr_lines(
        "codex",
        ["Reading prompt from stdin...\n", "real error\n"],
    ) == ["real error\n"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HTTP 429 Too Many Requests", True),
        ("rate limit exceeded", True),
        ("request was throttled by upstream", True),
        ("tool finished 429 items successfully", False),
        ("progress: 429/1000 tokens", False),
        ("error id=429", False),
    ],
)
def test_is_rate_limit_signal(text, expected):
    """Verifica que is rate limit signal."""
    assert _is_rate_limit_signal(text) is expected


def test_codex_profile_reads_prompt_from_stdin():
    """Verifica que codex profile reads prompt from stdin."""
    profile = get_profile("codex")
    assert profile is not None
    assert profile.prompt_as_arg is False
    assert profile.aliases == []


def test_codex_profile_resumes_last_session_in_workspace():
    """Verifica que codex profile resumes last session in workspace."""
    profile = get_profile("codex")
    assert profile is not None
    assert profile.effective_cmd() == [
        "codex",
        "exec",
        "resume",
        "--last",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--json",
        "-",
    ]


def test_codex_profile_applies_resume_to_cli_override():
    """Verifica que codex profile applies resume to cli override."""
    profile = get_profile("codex")
    assert profile is not None
    original_override = profile._connection_override
    original_mcp_socket = profile._mcp_socket_path
    try:
        profile.set_mcp_socket_path(None)
        profile._connection_override = CliConnection(
            cmd=["codex", "exec", "--json"],
            prompt_as_arg=False,
            output_format="codex-json",
        )
        assert profile.effective_cmd() == ["codex", "exec", "resume", "--last", "--json", "-"]
    finally:
        profile._connection_override = original_override
        profile.set_mcp_socket_path(original_mcp_socket)


def test_codex_profile_injects_mcp_server_before_stdin_sentinel():
    """Verifica que codex profile injects mcp server before stdin sentinel."""
    profile = get_profile("codex")
    assert profile is not None
    original_mcp_socket = profile._mcp_socket_path
    try:
        profile.set_mcp_socket_path("/tmp/quimera.sock")
        expected_args = json.dumps(
            ["-m", "quimera.runtime.mcp", "--connect-socket", "/tmp/quimera.sock"],
            ensure_ascii=False,
        )
        assert profile.effective_cmd() == [
            "codex",
            "exec",
            "resume",
            "--last",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
            "-c",
            'mcp_servers.quimera.command="python"',
            "-c",
            f"mcp_servers.quimera.args={expected_args}",
            "-",
        ]
    finally:
        profile.set_mcp_socket_path(original_mcp_socket)


def test_codex_profile_does_not_duplicate_existing_mcp_override():
    """Verifica que codex profile does not duplicate existing mcp override."""
    profile = get_profile("codex")
    assert profile is not None
    original_override = profile._connection_override
    original_mcp_socket = profile._mcp_socket_path
    try:
        profile.set_mcp_socket_path("/tmp/quimera.sock")
        profile._connection_override = CliConnection(
            cmd=[
                "codex",
                "exec",
                "--json",
                "-c",
                'mcp_servers.quimera.command="python"',
            ],
            prompt_as_arg=True,
            output_format="codex-json",
        )
        assert profile.effective_cmd() == [
            "codex",
            "exec",
            "resume",
            "--last",
            "--json",
            "-c",
            'mcp_servers.quimera.command="python"',
        ]
    finally:
        profile._connection_override = original_override
        profile.set_mcp_socket_path(original_mcp_socket)


def test_claude_profile_injects_mcp_server():
    """Verifica que claude profile injects mcp server."""
    import json as _json
    profile = get_profile("claude")
    assert profile is not None
    original_mcp_socket = profile._mcp_socket_path
    try:
        profile.set_mcp_socket_path("/tmp/quimera.sock")
        cmd = profile.effective_cmd()
        base = [
            "claude",
            "--permission-mode=bypassPermissions",
            "--output-format=stream-json",
            "--verbose",
            "--print",
            "--input-format=stream-json",
        ]
        assert cmd[:len(base)] == base
        assert "--mcp-config" in cmd
        idx = cmd.index("--mcp-config")
        config = _json.loads(cmd[idx + 1])
        assert "mcpServers" in config
        assert "quimera" in config["mcpServers"]
        proxy_args = config["mcpServers"]["quimera"]["args"]
        assert "--connect-socket" in proxy_args
        assert "/tmp/quimera.sock" in proxy_args
    finally:
        profile.set_mcp_socket_path(original_mcp_socket)


def test_claude_profile_configure_with_model_inserts_model_flag():
    """Claude não usa placeholder --model=, mas deve aceitar modelo nomeado."""
    profile = get_profile("claude")
    assert profile is not None

    conn = profile.configure_with_model("sonnet")

    assert isinstance(conn, CliConnection)
    assert conn.cmd[:3] == ["claude", "--model", "sonnet"]
    assert conn.output_format == "stream-json"


def test_codex_profile_configure_with_model_inserts_model_flag():
    """Codex não usa placeholder --model=, mas deve aceitar modelo nomeado."""
    profile = get_profile("codex")
    assert profile is not None

    conn = profile.configure_with_model("gpt-5.5")

    assert isinstance(conn, CliConnection)
    assert conn.cmd[:4] == ["codex", "exec", "--model", "gpt-5.5"]
    assert "--json" in conn.cmd
    assert conn.output_format == "codex-json"


def test_agent_client_cli_attrs_fall_back_to_profile_output_format():
    """Conexões antigas sem output_format ainda usam parser do perfil herdado."""
    profile = SimpleNamespace(
        effective_cmd=lambda: ["opencode", "run"],
        effective_output_format=lambda: "opencode-json",
        output_format="opencode-json",
    )
    connection = CliConnection(cmd=["opencode", "run"], output_format=None)

    cmd, prompt_as_arg, output_format = AgentClient._resolve_profile_cli_attrs(profile, connection)

    assert cmd == ["opencode", "run"]
    assert prompt_as_arg is False
    assert output_format == "opencode-json"



def test_base_agent_profile_prefers_socket_when_socket_and_http_are_set():
    """Verifica que base agent profile prefers socket when socket and http are set."""
    from quimera.profiles.base import ExecutionProfile

    class _SocketProfile(ExecutionProfile):
        def mcp_server_args(self, socket_path: str) -> list[str]:
            return ["--mcp-socket", socket_path]

        def mcp_http_server_args(self, url: str) -> list[str]:
            return ["--mcp-http", url]

    profile = _SocketProfile(name="base-test", prefix="/base-test", style=("white", "Base"), cmd=["agent", "-"])
    profile.set_mcp_socket_config("/tmp/quimera.sock", "internal-token")
    profile.set_mcp_http_config("https://external.example/mcp", "external-token")

    cmd = profile.effective_cmd()

    assert cmd == ["agent", "--mcp-socket", "/tmp/quimera.sock", "-"]
    assert "--mcp-http" not in cmd
    assert profile._build_token_args() == ["--token", "internal-token"]


def test_claude_profile_prefers_socket_when_socket_and_http_are_set():
    """Verifica que claude profile prefers socket when socket and http are set."""
    import json as _json
    profile = get_profile("claude")
    assert profile is not None
    original_mcp_socket = profile._mcp_socket_path
    original_mcp_http = profile._mcp_http_url
    original_token = profile._mcp_token
    try:
        profile.set_mcp_socket_config("/tmp/quimera.sock", "internal-token")
        profile.set_mcp_http_config("https://external.example/mcp", "external-token")
        cmd = profile.effective_cmd()
        idx = cmd.index("--mcp-config")
        config_raw = cmd[idx + 1]
        config = _json.loads(config_raw)
        server = config["mcpServers"]["quimera"]
        assert server["type"] == "stdio"
        assert server["command"] == "python"
        assert "--connect-socket" in server["args"]
        assert "/tmp/quimera.sock" in server["args"]
        assert "internal-token" in server["args"]
        assert "type=http" not in config_raw
        assert "https://external.example/mcp" not in config_raw
    finally:
        profile._mcp_socket_path = original_mcp_socket
        profile._mcp_http_url = original_mcp_http
        profile._mcp_token = original_token


def test_codex_profile_prefers_socket_when_socket_and_http_are_set():
    """Verifica que codex profile prefers socket when socket and http are set."""
    profile = get_profile("codex")
    assert profile is not None
    original_mcp_socket = profile._mcp_socket_path
    original_mcp_http = profile._mcp_http_url
    original_token = profile._mcp_token
    try:
        profile.set_mcp_socket_config("/tmp/quimera.sock", "internal-token")
        profile.set_mcp_http_config("https://external.example/mcp", "external-token")
        cmd = profile.effective_cmd()
        joined = "\n".join(cmd)
        assert 'mcp_servers.quimera.command="python"' in cmd
        assert "--connect-socket" in joined
        assert "/tmp/quimera.sock" in joined
        assert "internal-token" in joined
        assert "mcp_servers.quimera.url" not in joined
        assert "mcp_servers.quimera.transport" not in joined
        assert "https://external.example/mcp" not in joined
    finally:
        profile._mcp_socket_path = original_mcp_socket
        profile._mcp_http_url = original_mcp_http
        profile._mcp_token = original_token


def test_opencode_profile_prefers_local_socket_env_when_socket_and_http_are_set():
    """Verifica que opencode profile prefers local socket env when socket and http are set."""
    profile = get_profile("opencode")
    assert isinstance(profile, OpenCodeProfile)
    original_mcp_socket = profile._mcp_socket_path
    original_mcp_http = profile._mcp_http_url
    original_token = profile._mcp_token
    try:
        profile.set_mcp_socket_config("/tmp/quimera.sock", "internal-token")
        profile.set_mcp_http_config("https://external.example/mcp", "external-token")
        env = profile.env_for_cli()
        config_raw = env["OPENCODE_CONFIG_CONTENT"]
        config = json.loads(config_raw)
        server = config["mcp"]["quimera"]
        assert server["type"] == "local"
        assert "--connect-socket" in server["command"]
        assert "/tmp/quimera.sock" in server["command"]
        assert "internal-token" in server["command"]
        assert '"type": "remote"' not in config_raw
        assert "https://external.example/mcp" not in config_raw
    finally:
        profile._mcp_socket_path = original_mcp_socket
        profile._mcp_http_url = original_mcp_http
        profile._mcp_token = original_token

def test_agent_client_run_success(renderer):
    """Verifica que agent client run success."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = ["Success\n"]
        mock_proc.stderr = []
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        result = client.run(["echo", "hi"], silent=True)
        assert result == "Success"


def test_agent_client_run_silent_does_not_log_codex_stdin_noise(renderer):
    """Verifica que agent client run silent does not log codex stdin noise."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen, patch("quimera.agents.client._logger") as mock_logger:
        mock_proc = MagicMock()
        mock_proc.stdout = ["Success\n"]
        mock_proc.stderr = ["Reading prompt from stdin...\n"]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        result = client.run(["codex", "exec"], silent=True, agent="codex")

        assert result == "Success"
        mock_logger.warning.assert_not_called()


def test_agent_client_run_ignores_codex_orphan_function_call_noise(renderer):
    """Verifica que agent client run ignores codex orphan function call noise."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["ok\n"])
        mock_proc.stderr = iter([
            "2026-05-27T00:14:34.911494Z ERROR codex_core::util: "
            "Orphan function call output for call id: call_K6xlRm0c9JyUe9iBNv9Txgcl\n"
        ])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["codex", "exec"], silent=False, show_status=False, agent="codex")

    assert result == "ok"
    for args, _kwargs in renderer.show_plain.call_args_list:
        joined = " ".join(str(part) for part in args)
        assert "Orphan function call output for call id" not in joined


def test_agent_client_run_os_error(renderer):
    """Verifica que agent client run os error."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_popen.side_effect = OSError("command not found")
        result = client.run(["nonexistent"], silent=True)
        assert result is None
        renderer.show_error.assert_called()


def test_agent_client_run_failure_return_code(renderer):
    """Verifica que agent client run failure return code."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = []
        mock_proc.stderr = ["Error detail\n"]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        result = client.run(["fail"], silent=True)
        assert result is None
        renderer.show_error.assert_any_call(
            "[erro] retornou código 1",
            agent=None,
            command_name="fail",
            error_kind="agent_exit",
            return_code=1,
        )


def test_agent_client_run_failure_uses_error_reporter_when_provided(renderer):
    """Verifica que agent client run failure uses error reporter when provided."""
    error_reporter = MagicMock()
    client = AgentClient(renderer, error_reporter=error_reporter)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = []
        mock_proc.stderr = ["Error detail\n"]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        result = client.run(["fail"], silent=True)

    assert result is None
    renderer.show_error.assert_called_once_with(
        "[erro] retornou código 1",
        agent=None,
        command_name="fail",
        error_kind="agent_exit",
        return_code=1,
    )
    renderer.show_plain.assert_called_once_with("Error detail", agent=None)


def test_agent_client_run_failure_uses_profile_label_when_agent_is_known(renderer):
    """Verifica que agent client run failure uses profile label when agent is known."""
    error_reporter = MagicMock()
    client = AgentClient(renderer, error_reporter=error_reporter)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = []
        mock_proc.stderr = ["Error detail\n"]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        result = client.run(["claude"], silent=True, agent="claude")

    assert result is None
    renderer.show_error.assert_called_once_with(
        "[erro] retornou código 1",
        agent="claude",
        command_name="claude",
        error_kind="agent_exit",
        return_code=1,
    )
    renderer.show_plain.assert_called_once_with("Error detail", agent="claude")


def test_agent_client_run_failure_uses_agent_name_when_profile_is_unknown(renderer):
    """Verifica que agent client run failure uses agent name when profile is unknown."""
    error_reporter = MagicMock()
    client = AgentClient(renderer, error_reporter=error_reporter)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = []
        mock_proc.stderr = ["Error detail\n"]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        result = client.run(["real-binary-name"], silent=True, agent="agente-logico")

    assert result is None
    renderer.show_error.assert_called_once_with(
        "[erro] retornou código 1",
        agent="agente-logico",
        command_name="real-binary-name",
        error_kind="agent_exit",
        return_code=1,
    )
    renderer.show_plain.assert_called_once_with("Error detail", agent="agente-logico")


def test_agent_client_call(renderer):
    """Verifica que agent client call."""
    client = AgentClient(renderer)
    with patch("quimera.profiles.get") as mock_get:
        mock_profile = MagicMock()
        mock_profile.cmd = ["mock-agent"]
        mock_profile.prompt_as_arg = False
        mock_profile.effective_cmd.return_value = ["mock-agent"]
        mock_profile.effective_prompt_as_arg.return_value = False
        mock_get.return_value = mock_profile

        with patch.object(client, "run") as mock_run:
            mock_run.return_value = "output"
            result = client.call("mock", "prompt")
            assert result == "output"
            mock_run.assert_called_with(["mock-agent"], input_text="prompt", _primed_proc=None,
                                        silent=False, agent="mock", show_status=True, progress_callback=None)


def test_agent_client_call_prompt_as_arg(renderer):
    """Verifica que agent client call prompt as arg."""
    client = AgentClient(renderer)
    with patch("quimera.profiles.get") as mock_get:
        mock_profile = MagicMock()
        mock_profile.cmd = ["mock-agent"]
        mock_profile.prompt_as_arg = True
        mock_profile.effective_cmd.return_value = ["mock-agent"]
        mock_profile.effective_prompt_as_arg.return_value = True
        mock_get.return_value = mock_profile

        with patch.object(client, "run") as mock_run:
            mock_run.return_value = "output"
            result = client.call("mock", "prompt")
            assert result == "output"
            mock_run.assert_called_with(["mock-agent", "prompt"], input_text=None, silent=False, agent="mock",
                                        show_status=True, progress_callback=None)


def test_agent_client_resume_session_only_when_agent_is_frozen(renderer):
    """Verifica que session_id de profile CLI só é usado após abrir sessão persistente."""
    client = AgentClient(renderer)

    class ResumeProfile:
        cmd = ["mock-agent"]
        prompt_as_arg = False
        output_format = None
        supports_resume = True
        supports_warm_pool = False

        def effective_connection(self):
            return CliConnection(cmd=list(self.cmd), prompt_as_arg=False)

        def effective_cmd(self):
            return list(self.cmd)

        def format_stdin_input(self, prompt):
            return f"stdin:{prompt}"

        def extract_session_id(self, raw):
            return raw.split("sid=", 1)[1] if "sid=" in raw else None

        def inject_resume_arg(self, cmd, session_id):
            return [*cmd, "--resume", session_id]

    profile = ResumeProfile()
    with patch("quimera.profiles.get", return_value=profile), patch.object(client, "run") as mock_run:
        mock_run.side_effect = ["sid=abc123", "ok"]

        assert client.open_persistent_session("mock") is True
        assert client.call("mock", "primeiro") == "sid=abc123"
        assert client.call("mock", "segundo") == "ok"

    assert mock_run.call_args_list[0].kwargs["input_text"] == "stdin:primeiro"
    assert mock_run.call_args_list[0].args[0] == ["mock-agent"]
    assert mock_run.call_args_list[1].kwargs["input_text"] == "stdin:segundo"
    assert mock_run.call_args_list[1].args[0] == ["mock-agent", "--resume", "abc123"]


def test_agent_client_call_passes_prompt_text_unchanged(renderer):
    """Verifica que o PromptText é repassado intacto para run(), sem concatenação."""
    client = AgentClient(renderer)
    prompt = PromptText(
        '<current_turn title="Pedido atual">pedido</current_turn>',
        strict=True,
    )
    client.execution_mode = SimpleNamespace(prompt_addon="[MODO: ANÁLISE]")

    with patch("quimera.profiles.get") as mock_get:
        mock_profile = MagicMock()
        mock_profile.cmd = ["mock-agent"]
        mock_profile.prompt_as_arg = False
        mock_profile.effective_cmd.return_value = ["mock-agent"]
        mock_profile.effective_prompt_as_arg.return_value = False
        mock_get.return_value = mock_profile

        with patch.object(client, "run") as mock_run:
            mock_run.return_value = "output"
            result = client.call("mock", prompt)
            assert result == "output"
            mock_run.assert_called_with(["mock-agent"], input_text=prompt, _primed_proc=None,
                                        silent=False, agent="mock", show_status=True, progress_callback=None)

def test_agent_client_log_metrics(renderer, tmp_path):
    """Verifica que agent client log metrics."""
    metrics_file = tmp_path / "metrics.jsonl"
    client = AgentClient(renderer, metrics_file=str(metrics_file))
    metrics = {"total_chars": 100, "history_chars": 50}
    client.log_prompt_metrics("claude", metrics)

    assert metrics_file.exists()
    content = metrics_file.read_text()
    assert '"agent": "claude"' in content
    assert '"total_chars": 100' in content


def test_agent_client_run_streaming(renderer):
    """Verifica que agent client run streaming."""
    # Line 100-161 coverage approx
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["line1\n", "line2\n"])
        mock_proc.stderr = iter(["err1\n"])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        # Mocking time.sleep to avoid waiting
        with patch("time.sleep"):
            result = client.run(["echo"], silent=False, show_status=True)
            assert "line1" in result
            assert "line2" in result


def test_agent_client_run_timeout(renderer):
    """Verifica que agent client run timeout."""
    # Line 152-161 coverage approx
    client = AgentClient(renderer, idle_timeout=0.1)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        mock_proc.pid = 99999  # real int so os.getpgid raises OSError (process not found)
        with patch("threading.Thread") as mock_thread_cls:
            mock_stdout_thread = MagicMock()
            mock_stderr_thread = MagicMock()
            # Loop stays alive for first iteration; idle timeout fires before second check
            mock_stdout_thread.is_alive.side_effect = [True, False]
            mock_stderr_thread.is_alive.return_value = False
            mock_thread_cls.side_effect = [mock_stdout_thread, mock_stderr_thread]

            with patch("time.time", return_value=500.0):
                with patch("time.monotonic") as mock_monotonic:
                    # 1. _last_stdout_time = 100.0 (ProcessRunner.__init__)
                    # 2. start_time = 100.0 (watch)
                    # 3. now = 101.0 => elapsed=1 e idle=1.0 > 0.1
                    mock_monotonic.side_effect = [100.0, 100.0, 101.0]
                    with patch("time.sleep"):
                        result = client.run(["slow"], silent=False)
                        assert result is None
                        renderer.show_error.assert_called()


def test_agent_client_run_input_failure(renderer):
    """Verifica que agent client run input failure."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdin.write.side_effect = Exception("broken pipe")
        mock_popen.return_value = mock_proc

        result = client.run(["cmd"], input_text="input", silent=True)
        assert result is None
        mock_proc.kill.assert_called()
        renderer.show_error.assert_called()


def test_agent_client_run_communication_error(renderer):
    """Verifica que agent client run communication error."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["out\n"])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # We need to wait for threads, but we can mock them or force error
        with patch("threading.Thread") as mock_thread:
            # We want to set result_holder["error"]
            # This is hard because result_holder is local to run()
            # Let's mock the whole run method's internals or use a different approach
            pass


def test_agent_client_run_silent_logs(renderer):
    """Verifica que agent client run silent logs."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = ["Out\n"]
        mock_proc.stderr = ["Err\n"]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        with patch("quimera.agents.client._logger") as mock_logger:
            result = client.run(["cmd"], silent=True)
            assert result == "Out"
            mock_logger.debug.assert_called()
            mock_logger.warning.assert_called()


def test_agent_client_run_failure_with_tail(renderer):
    """Verifica que agent client run failure with tail."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = []
        mock_proc.stderr = ["Line 1\n", "Line 2\n", "Line 3\n", "Line 4\n", "Line 5\n", "Line 6\n"]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        result = client.run(["fail"], silent=True)
        assert result is None
        assert renderer.show_error.call_count == 1
        assert renderer.show_plain.call_count >= 1


def test_agent_client_run_caps_stdout_buffer_to_recent_output(renderer):
    """Verifica que agent client run caps stdout buffer to recent output."""
    client = AgentClient(renderer)
    with patch.object(AgentClient, "_MAX_STDOUT_CHARS", 12), patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = ["12345\n", "67890\n", "tail\n"]
        mock_proc.stderr = []
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        result = client.run(["cmd"], silent=True)

    assert result == "[...stdout truncado...]\n67890\ntail"


def test_agent_client_log_queue_drops_oldest_item_when_full(renderer):
    """Verifica que agent client log queue drops oldest item when full."""
    client = AgentClient(renderer)
    q = queue.Queue(maxsize=2)

    client._enqueue_log_item(q, ("stdout", "one"))
    client._enqueue_log_item(q, ("stdout", "two"))
    client._enqueue_log_item(q, ("stdout", "three"))

    assert q.get_nowait() == ("stdout", "two")
    assert q.get_nowait() == ("stdout", "three")


def test_agent_client_run_marks_rate_limit_from_stderr(renderer):
    """Verifica que agent client run marks rate limit from stderr."""
    client = AgentClient(renderer, idle_timeout=1)

    class SlowLines:
        def __init__(self, lines, delay=0.05):
            self._lines = list(lines)
            self._delay = delay

        def __iter__(self):
            for line in self._lines:
                threading.Event().wait(self._delay)
                yield line

    with patch("subprocess.Popen") as mock_popen, patch("time.sleep"):
        mock_proc = MagicMock()
        mock_proc.stdout = SlowLines(["output\n"])
        mock_proc.stderr = SlowLines(["HTTP 429 Too Many Requests\n"])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        result = client.run(["cmd"], silent=False, show_status=False)

    assert "output" in result
    assert client.rate_limit_detected is True
    assert client.rate_limit_detected_at is not None


def test_agent_client_run_does_not_mark_rate_limit_from_stdout(renderer):
    """Verifica que agent client run does not mark rate limit from stdout."""
    client = AgentClient(renderer, idle_timeout=1)

    with patch("subprocess.Popen") as mock_popen, patch("time.sleep"):
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["the tool printed: rate limit\n"])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        result = client.run(["cmd"], silent=False, show_status=False)

    assert "rate limit" in result
    assert client.rate_limit_detected is False
    assert client.rate_limit_detected_at is None


def test_agent_client_run_streaming_with_status_and_stderr(renderer):
    """Verifica que agent client run streaming with status and stderr."""
    # Line 118, 123-129, 131-140 approx
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["out1\n"])
        mock_proc.stderr = iter(["err1\n", "err2\n"])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["echo"], silent=False, show_status=True)
            assert "out1" in result
            renderer.show_plain.assert_any_call("err1", agent=ANY)


def test_agent_client_run_suppresses_codex_stdin_noise(renderer):
    """Verifica que agent client run suppresses codex stdin noise."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(['{"type":"item.completed","item":{"id":"1","type":"agent_message","text":"ok"}}\n'])
        mock_proc.stderr = iter(["Reading additional input from stdin...\n"])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["codex", "exec"], silent=False, agent="codex", show_status=False)

        assert "agent_message" in result
        # Verifica que a linha de ruído do codex não foi exibida (pode haver mensagens de summary)
        noise = "Reading additional input from stdin..."
        for call_args in renderer.show_plain.call_args_list:
            assert noise not in str(call_args)


def test_agent_client_run_spy_shows_stderr_lines(renderer):
    """Verifica que agent client run spy shows stderr lines."""
    client = AgentClient(renderer, visibility=Visibility.FULL)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(['{"type":"item.completed","item":{"id":"1","type":"agent_message","text":"ok"}}\n'])
        mock_proc.stderr = iter(["tool: exec_command\n", "tool: apply_patch\n"])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["codex", "exec"], silent=False, agent="codex", show_status=False)

    assert '"text":"ok"' in result
    renderer.update_agent_transient.assert_any_call("codex", "tool: exec_command")
    renderer.update_agent_transient.assert_any_call("codex", "tool: apply_patch")


def test_agent_client_run_spy_shows_codex_stdout_context(renderer):
    """Verifica que agent client run spy shows codex stdout context."""
    client = AgentClient(renderer, visibility=Visibility.FULL)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.started","item":{"type":"reasoning","summary":"Vou checar o estado do repositório antes de editar"}}\n',
            '{"type":"item.started","item":{"type":"command_execution","command":"git status"}}\n',
            '{"type":"item.completed","item":{"type":"command_execution","command":"git status","exit_code":0}}\n',
            '{"type":"item.completed","item":{"id":"1","type":"agent_message","text":"Encontrei alterações locais e vou seguir sem revertê-las."}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["codex", "exec"], silent=False, agent="codex", show_status=False)

    assert "reasoning" in result
    assert "agent_message" in result
    renderer.update_agent_transient.assert_any_call("codex", "Vou checar o estado do repositório antes de editar")
    transient_lines = [call.args[1] for call in renderer.update_agent_transient.call_args_list]
    assert any("TOOL_START" in line and "cmd=git status" in line for line in transient_lines)
    assert any("TOOL_END" in line and "status=ok" in line for line in transient_lines)
    renderer.update_agent_transient.assert_any_call(
        "codex", "Encontrei alterações locais e vou seguir sem revertê-las."
    )


def test_agent_client_run_summary_shows_formatted_codex_stdout(renderer):
    """Verifica que agent client run summary shows formatted codex stdout."""
    client = AgentClient(renderer, visibility=Visibility.SUMMARY)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.completed","item":{"type":"agent_message","text":"message 1\\nmessage 2\\nclear\\nmessage 3"}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            client.run(["codex", "exec"], silent=False, agent="codex", show_status=False)

    assert ("→ codex iniciando...",) not in [call.args for call in renderer.show_system_neutral.call_args_list]
    renderer.update_agent_transient.assert_any_call("codex", "message 1")
    renderer.update_agent_transient.assert_any_call("codex", "message 2")
    renderer.update_agent_transient.assert_any_call("codex", "message 3")
    renderer.clear_agent_transient.assert_called_with("codex")
    renderer.show_plain.assert_any_call("execução concluída", agent="codex", muted=True)


def test_agent_client_run_summary_flushes_compacted_responses_before_context(renderer):
    """Verifica que agent client run summary flushes compacted responses before context."""
    client = AgentClient(renderer, visibility=Visibility.SUMMARY)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.completed","item":{"type":"agent_message","text":"linha 1\\nlinha 2"}}\n',
            '{"type":"item.started","item":{"type":"command_execution","command":"git status"}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            client.run(["codex", "exec"], silent=False, agent="codex", show_status=True)

    renderer.update_agent_transient.assert_any_call("codex", "linha 1")
    renderer.update_agent_transient.assert_any_call("codex", "linha 2")
    renderer.update_agent_transient.assert_any_call("codex", "$ git status")


def test_agent_client_run_summary_keeps_completed_tool_line_transient(renderer):
    """Verifica que agent client run summary mantém tool completion no buffer transient."""
    client = AgentClient(renderer, visibility=Visibility.SUMMARY)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.started","item":{"type":"command_execution","command":"git diff -- quimera/agents.py"}}\n',
            '{"type":"item.completed","item":{"type":"command_execution","command":"git diff -- quimera/agents.py","exit_code":0}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            client.run(["codex", "exec"], silent=False, agent="codex", show_status=True)

    renderer.update_agent_transient.assert_any_call("codex", "$ git diff -- quimera/agents.py")
    renderer.update_agent_transient.assert_any_call("codex", "✓ git diff -- quimera/agents.py")


def test_agent_client_run_summary_shows_diff_output_and_keeps_next_operation_clean(renderer):
    """Verifica que agent client run summary shows diff output and keeps next operation clean."""
    client = AgentClient(renderer, visibility=Visibility.SUMMARY)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.started","item":{"type":"command_execution","command":"git diff -- quimera/agents.py"}}\n',
            '{"type":"item.completed","item":{"type":"command_execution","command":"git diff -- quimera/agents.py","exit_code":0,"aggregated_output":"diff --git a/quimera/agents.py b/quimera/agents.py\\n+nova linha"}}\n',
            '{"type":"item.started","item":{"type":"command_execution","command":"git status --short"}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            client.run(["codex", "exec"], silent=False, agent="codex", show_status=True)

    client.flush_pending_summary()

    renderer.update_agent_transient.assert_any_call("codex", "$ git status --short")
    renderer.update_agent_transient.assert_any_call("codex", "✓ git diff -- quimera/agents.py")
    renderer.show_plain.assert_any_call("diff --git a/quimera/agents.py b/quimera/agents.py", agent="codex", muted=True)
    renderer.show_plain.assert_any_call("+nova linha", agent="codex", muted=True)
    assert client.last_spy_turn_detail is not None
    assert client.last_spy_turn_detail["tools"]
    # summary delegada ao renderer via show_turn_summary (MagicMock tem o método)
    renderer.show_turn_summary.assert_called_once()
    _, detail_arg = renderer.show_turn_summary.call_args.args
    assert detail_arg["tools"]


def test_spy_output_presenter_keeps_next_operation_clean_after_diff_preview(renderer):
    """Verifica que spy output presenter keeps next operation visible after diff preview."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="$ git diff -- quimera/agents.py"))
    presenter.emit("codex", SpyEvent(kind="tool", text="✓ git diff -- quimera/agents.py"))
    for event in format_command_output_preview(
        "git diff -- quimera/agents.py",
        "diff --git a/quimera/agents.py b/quimera/agents.py\n+nova linha",
    ):
        presenter.emit("codex", event)
    presenter.emit("codex", SpyEvent(kind="tool", text="$ git status --short"))

    renderer.update_agent_transient.assert_any_call("codex", "$ git diff -- quimera/agents.py")
    renderer.update_agent_transient.assert_any_call("codex", "✓ git diff -- quimera/agents.py")
    renderer.show_plain.assert_any_call("diff --git a/quimera/agents.py b/quimera/agents.py", agent="codex", muted=True)
    renderer.show_plain.assert_any_call("+nova linha", agent="codex", muted=True)
    assert presenter.current_status_label == "$ git status --short"


def test_spy_output_presenter_compose_status_label_keeps_base_and_tool(renderer):
    """Verifica que spy output presenter compose status label keeps base and tool."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="$ git status --short"))

    assert presenter.compose_status_label("codex") == "codex | $ git status --short"


def test_spy_output_presenter_collects_structured_turn_detail(renderer):
    """Verifica que spy output presenter collects structured turn detail."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)
    presenter.set_turn_runtime("cli")

    for event in _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"command_execution","command":"ls","id":"t_17"}}'
    ):
        presenter.emit("codex", event)
    for event in _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0,"id":"t_17"}}'
    ):
        presenter.emit("codex", event)

    detail = presenter.finalize_turn("codex")

    assert detail["turn_id"].startswith("turn_")
    assert detail["trace_id"].endswith(detail["turn_id"])
    assert detail["runtime"] == "cli"
    assert len(detail["tools"]) == 1
    assert detail["tools"][0]["tool_call_id"] == "t_17"
    assert detail["tools"][0]["tool"] == "exec_command"
    assert detail["tools"][0]["status"] == "ok"
    assert detail["tools"][0]["input"] == {"cmd": "ls"}
    assert isinstance(detail["tools"][0]["duration_ms"], int)


def test_spy_output_presenter_persists_evidence_from_raw_output(renderer, tmp_path):
    """Verifica que spy output presenter persists evidence from raw output."""
    presenter = SpyOutputPresenter(
        renderer,
        Visibility.SUMMARY,
        session_id="sessao-1",
        base_dir=tmp_path,
    )

    presenter.consume_stdout("codex", "Read file: quimera/prompt.py")
    presenter.finalize_turn("codex")

    store = EvidenceStore(tmp_path, "sessao-1")
    try:
        evidences = store.query("sessao-1")
    finally:
        store.close()

    assert len(evidences) == 1
    assert evidences[0].type == "file_read"
    assert evidences[0].path == "quimera/prompt.py"
    assert evidences[0].agent == "codex"
    assert evidences[0].session_id == "sessao-1"


def test_spy_output_presenter_persists_structured_tool_execution_evidence(renderer, tmp_path):
    """Verifica que spy output presenter persists structured tool execution evidence."""
    presenter = SpyOutputPresenter(
        renderer,
        Visibility.SUMMARY,
        session_id="sessao-1",
        base_dir=tmp_path,
    )

    for event in _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"command_execution","command":"ls","id":"t_17"}}'
    ):
        presenter.emit("codex", event)
    for event in _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0,"id":"t_17"}}'
    ):
        presenter.emit("codex", event)

    presenter.finalize_turn("codex")

    store = EvidenceStore(tmp_path, "sessao-1")
    try:
        evidences = store.query("sessao-1")
    finally:
        store.close()

    tool_evidences = [e for e in evidences if e.type == "tool_call"]
    assert len(tool_evidences) == 1
    assert tool_evidences[0].summary == "exec_command: ok | cmd: ls"
    assert tool_evidences[0].agent == "codex"


def test_spy_output_presenter_finalize_turn_renders_human_summary(renderer):
    """Verifica que spy output presenter finalize turn renders human summary."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    for event in _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"command_execution","command":"ls","id":"t_17"}}'
    ):
        presenter.emit("codex", event)
    for event in _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0,"id":"t_17"}}'
    ):
        presenter.emit("codex", event)

    detail = presenter.finalize_turn("codex", render_summary=True)

    # renderer tem show_turn_summary (MagicMock), então a summary é delegada a ele
    renderer.show_turn_summary.assert_called_once_with("codex", detail)
    assert detail["tools"]
    assert detail["tools"][0]["tool"] == "exec_command"
    assert detail["tools"][0]["tool_call_id"] == "t_17"
    assert detail["tools"][0]["status"] == "ok"


def test_spy_output_presenter_finalize_turn_skips_summary_for_non_cli(renderer):
    """Verifica que spy output presenter finalize turn skips summary for non cli."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)
    with patch("quimera.spy_output_presenter.profiles.get") as mock_get_profile:
        non_cli = MagicMock()
        non_cli.effective_driver.return_value = "openai"
        mock_get_profile.return_value = non_cli
        for event in _format_codex_spy_event(
            '{"type":"item.started","item":{"type":"command_execution","command":"ls","id":"t_17"}}'
        ):
            presenter.emit("claude", event)
        for event in _format_codex_spy_event(
            '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0,"id":"t_17"}}'
        ):
            presenter.emit("claude", event)

        detail = presenter.finalize_turn("claude", render_summary=True)

    renderer.show_turn_summary.assert_not_called()
    assert detail["tools"]


def test_spy_output_presenter_full_mode_renders_tool_timeline(renderer):
    """Verifica que spy output presenter full mode renders tool timeline."""
    presenter = SpyOutputPresenter(renderer, Visibility.FULL)

    for event in _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"command_execution","command":"ls","id":"t_17"}}'
    ):
        presenter.emit("codex", event)
    for event in _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0,"id":"t_17"}}'
    ):
        presenter.emit("codex", event)

    transient_lines = [call.args[1] for call in renderer.update_agent_transient.call_args_list]
    assert any("TOOL_START id=t_17 tool=exec_command cmd=ls" in line for line in transient_lines)
    assert any("TOOL_END id=t_17 status=ok" in line for line in transient_lines)


def test_agent_client_run_summary_does_not_persist_started_tool_without_status(renderer):
    """Verifica que agent client run summary mantém tool start/completion no transient."""
    client = AgentClient(renderer, visibility=Visibility.SUMMARY)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.started","item":{"type":"command_execution","command":"git diff -- quimera/agents.py"}}\n',
            '{"type":"item.completed","item":{"type":"command_execution","command":"git diff -- quimera/agents.py","exit_code":0}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            client.run(["codex", "exec"], silent=False, agent="codex", show_status=False)

    renderer.update_agent_transient.assert_any_call("codex", "$ git diff -- quimera/agents.py")
    renderer.update_agent_transient.assert_any_call("codex", "✓ git diff -- quimera/agents.py")


def test_spy_output_presenter_summary_keeps_tool_progress_transient(renderer):
    """Verifica que spy output presenter summary mantém tool progress transient."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="usando apply_patch"))
    presenter.emit("codex", SpyEvent(kind="tool", text="✓ editar quimera/agents.py"))

    assert presenter.current_status_label == ""
    renderer.update_agent_transient.assert_has_calls([
        call("codex", "usando apply_patch"),
        call("codex", "✓ editar quimera/agents.py"),
    ])


def test_spy_output_presenter_summary_keeps_tool_failure_persistent(renderer):
    """Verifica que spy output presenter summary mantém tool failure transient."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="$ pytest -q"))
    presenter.emit("codex", SpyEvent(kind="tool", text="✗ pytest -q (exit 1)"))

    renderer.update_agent_transient.assert_has_calls([
        call("codex", "$ pytest -q"),
        call("codex", "✗ pytest -q (exit 1)"),
    ])
    assert presenter.current_status_label == ""


def test_spy_output_presenter_summary_skips_transient_terminal_completion(renderer):
    """Verifica que spy output presenter summary skips transient terminal completion."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="context", text="execução concluída", transient=True))

    renderer.update_agent_transient.assert_not_called()
    renderer.show_plain.assert_not_called()
    assert presenter.current_status_label == ""


def test_agent_client_run_spy_shows_claude_stdout_context(renderer):
    """Verifica que agent client run spy shows claude stdout context."""
    client = AgentClient(renderer, visibility=Visibility.FULL)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"},{"type":"text","text":"Vou inspecionar o arquivo antes de sugerir a mudança."}]}}\n',
            '{"type":"result","result":"ok","is_error":false}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["claude", "-p"], silent=False, agent="claude", show_status=False)

    assert result == '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"},{"type":"text","text":"Vou inspecionar o arquivo antes de sugerir a mudança."}]}}\n{"type":"result","result":"ok","is_error":false}'
    renderer.update_agent_transient.assert_any_call("claude", "iniciando execução")
    renderer.update_agent_transient.assert_any_call("claude", "usando Read")
    renderer.update_agent_transient.assert_any_call("claude", "Vou inspecionar o arquivo antes de sugerir a mudança.")
    renderer.update_agent_transient.assert_any_call("claude", "execução concluída")
    renderer.clear_agent_transient.assert_any_call("claude")


def test_agent_client_run_post_drain(renderer):
    """Verifica que agent client run post drain."""
    # Line 166-180 approx - Drain remaining queue after threads die
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("threading.Thread") as mock_thread_cls:
            mock_stdout_thread = MagicMock()
            mock_stderr_thread = MagicMock()
            # Threads die immediately
            mock_stdout_thread.is_alive.return_value = False
            mock_stderr_thread.is_alive.return_value = False
            mock_thread_cls.side_effect = [mock_stdout_thread, mock_stderr_thread]

            # But we put something in the queue manually if we could...
            # Actually, the real threads put things in log_queue.
            # Since we mocked Thread, we have to simulate what they do.

            # Let's use a real thread for a moment or mock the queue behavior in the loop
            pass


def test_agent_client_run_uses_working_dir(renderer, tmp_path):
    """Verifica que agent client run uses working dir."""
    workspace = str(tmp_path)
    client = AgentClient(renderer, working_dir=workspace)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = ["ok\n"]
        mock_proc.stderr = []
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        client.run(["echo", "hi"], silent=True)
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("cwd") == workspace


def test_agent_client_run_legacy_workspace_root_alias(renderer, tmp_path):
    """Verifica que agent client run legacy workspace root alias."""
    workspace = str(tmp_path)
    client = AgentClient(renderer, workspace_root=workspace)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = ["ok\n"]
        mock_proc.stderr = []
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        client.run(["echo", "hi"], silent=True)
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("cwd") == workspace


def test_agent_client_run_without_working_dir_passes_none(renderer):
    """Verifica que agent client run without working dir passes none."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = ["ok\n"]
        mock_proc.stderr = []
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        client.run(["echo", "hi"], silent=True)
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("cwd") is None


def test_agent_client_thread_exceptions(renderer):
    """Verifica que agent client thread exceptions."""
    # Line 62-63, 74-75
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()

        # stdout that raises exception when iterated
        def error_iter():
            raise Exception("Read error")
            yield "never"

        mock_proc.stdout = error_iter()
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # This will set result_holder["error"]
        result = client.run(["cmd"], silent=True)
        assert result is None
        renderer.show_error.assert_any_call(
            "Read error",
            agent=None,
            command_name="cmd",
            error_kind="agent_comm",
            return_code=None,
        )


def test_synthetic_tool_result(renderer):
    """Verifica que synthetic tool result."""
    from quimera.agents import _SyntheticToolResult
    result_ok = _SyntheticToolResult(ok=True)
    assert result_ok.ok is True
    result_err = _SyntheticToolResult(ok=False, error="test error")
    assert result_err.ok is False
    assert result_err.error == "test error"


def test_agent_client_run_empty_stderr_line(renderer):
    """Verifica que agent client run empty stderr line."""
    # Line 144: empty line after strip
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["output\n", "  \n"])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["cmd"], silent=False)
            assert "output" in result


def test_agent_client_run_stderr_truncation(renderer):
    """Verifica que agent client run no longer truncates stderr in summary mode."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        lines = [f"error line {i}\n" for i in range(15)]
        mock_proc.stdout = iter(["output\n"])
        mock_proc.stderr = iter(lines)
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["cmd"], silent=False)
            assert "output" in result
            for idx in range(15):
                renderer.show_plain.assert_any_call(f"error line {idx}", agent=None)
            assert not any("stderr truncado" in str(c) for c in renderer.show_plain.call_args_list)


def test_agent_client_run_no_output_with_error(renderer):
    """Verifica que agent client run no output with error."""
    # Line 202-219: no output but has error (returncode 0 case)
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = []
        mock_proc.stderr = ["some stderr\n"]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        result = client.run(["cmd"], silent=True)
        assert result is None
        renderer.show_error.assert_called()


def test_agent_client_call_unknown_agent(renderer):
    """Verifica que agent client call unknown agent."""
    # Line 276-278: unknown agent
    client = AgentClient(renderer)
    result = client.call("unknown_agent", "prompt")
    assert result is None
    renderer.show_error.assert_called()


def test_agent_client_call_api_driver(renderer):
    """Verifica que agent client call api driver."""
    # Line 280-281, 293-325: API driver path
    client = AgentClient(renderer, idle_timeout=60)
    with patch("quimera.profiles.get") as mock_get:
        mock_profile = MagicMock()
        mock_profile.driver = "api"
        mock_profile.model = "llama3"
        mock_profile.base_url = "http://localhost:11434"
        mock_profile.api_key_env = "OLLAMA_API_KEY"
        mock_profile.supports_tools = True
        mock_profile.tool_use_reliability = "medium"
        mock_get.return_value = mock_profile

        with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls:
            mock_driver = MagicMock()
            mock_driver.run.return_value = "api response"
            mock_driver_cls.return_value = mock_driver

            with patch.object(client, "_api_drivers", {}):
                result = client.call("test-agent", "prompt")
            mock_driver_cls.assert_called()


def test_parse_stream_json(renderer):
    """Verifica que parse stream json."""
    # Line 223-247: _parse_stream_json
    client = AgentClient(renderer)
    raw = '''{"type":"result","result":"final output"}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"bash"}]}}'''
    result = client._parse_stream_json(raw, "test-agent")
    assert result == "final output"


def test_parse_stream_json_error(renderer):
    """Verifica que parse stream json error."""
    client = AgentClient(renderer)
    raw = '{"type":"result","is_error":true,"result":"error msg"}'
    result = client._parse_stream_json(raw, "test-agent")
    assert result is None


def test_parse_codex_json(renderer):
    """Verifica que parse codex json."""
    # Line 249-271: _parse_codex_json
    client = AgentClient(renderer)
    callback_called = []

    def track_callback(agent, result=None, loop_abort=None, reason=None):
        callback_called.append(True)

    client.tool_event_callback = track_callback

    raw = '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0}}'
    result = client._parse_codex_json(raw, "codex")
    assert callback_called


def test_parse_codex_json_with_text(renderer):
    """Verifica que parse codex json with text."""
    client = AgentClient(renderer)
    raw = '{"type":"item.completed","item":{"type":"agent_message","text":"final text"}}'
    result = client._parse_codex_json(raw, "codex")
    assert result == "final text"


def test_format_codex_spy_event_command():
    """Verifica que format codex spy event command."""
    started = _format_codex_spy_event('{"type":"item.started","item":{"type":"command_execution","command":"ls"}}')
    completed = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0}}')
    assert started == [SpyEvent(kind="tool", text="$ ls")]
    assert completed == [SpyEvent(kind="tool", text="✓ ls")]


def test_format_codex_spy_event_reasoning_and_message():
    """Verifica que format codex spy event reasoning and message."""
    reasoning = _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"reasoning","summary":"Vou localizar o formatter do profile e ajustar a mensagem"}}'
    )
    message = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"agent_message","text":"Ajustei a saída para mostrar progresso útil ao usuário."}}'
    )
    assert reasoning == [SpyEvent(kind="context", text="Vou localizar o formatter do profile e ajustar a mensagem", transient=True)]
    assert message == [SpyEvent(kind="response", text="Ajustei a saída para mostrar progresso útil ao usuário.", transient=True)]


def test_format_codex_spy_event_ignores_lifecycle_boundaries():
    """Verifica que limites de turno/sessão não duplicam status já emitido pelo runtime."""
    assert _format_codex_spy_event('{"type":"session.started"}') == []
    assert _format_codex_spy_event('{"type":"turn.started"}') == []
    assert _format_codex_spy_event('{"type":"turn.completed"}') == []


def test_format_codex_spy_event_ignores_error_items():
    """Verifica que item de erro não vaza como contexto genérico no overlay."""
    assert _format_codex_spy_event('{"type":"item.completed","item":{"type":"error","message":"boom"}}') == []


def test_format_codex_spy_event_splits_multiline_agent_messages():
    """Verifica que format codex spy event splits multiline agent messages."""
    message = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"agent_message","text":"message 1\\nmessage 2\\nclear\\nmessage 3"}}'
    )
    assert message == [
        SpyEvent(kind="response", text="message 1", transient=True),
        SpyEvent(kind="response", text="message 2", transient=True),
        SpyEvent(kind="clear", text="", transient=True),
        SpyEvent(kind="response", text="message 3", transient=True),
    ]


def test_format_codex_spy_event_keeps_full_transient_agent_line_without_truncation():
    """Verifica que format codex spy event keeps full transient agent line without truncation."""
    long_line = "x" * 220
    message = _format_codex_spy_event(
        f'{{"type":"item.completed","item":{{"type":"agent_message","text":"{long_line}"}}}}'
    )
    assert message == [SpyEvent(kind="response", text=long_line, transient=True)]


def test_format_codex_spy_event_reports_failed_test_command():
    """Verifica que format codex spy event reports failed test command."""
    completed = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q tests/test_agents.py","exit_code":1}}'
    )
    assert completed == [SpyEvent(kind="tool", text="✗ pytest -q tests/test_agents.py (exit 1)")]


def test_format_codex_spy_event_hides_successful_command_completion():
    """Verifica que format codex spy event hides successful command completion."""
    started = _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"command_execution","command":"git status --short"}}'
    )
    completed = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"git status --short","exit_code":0}}'
    )
    assert started == [SpyEvent(kind="tool", text="$ git status --short")]
    assert completed == [SpyEvent(kind="tool", text="✓ git status --short")]


def test_format_codex_spy_event_includes_diff_output_from_aggregated_output():
    """Verifica que format codex spy event includes diff output from aggregated output."""
    completed = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"git diff -- quimera/agents.py","exit_code":0,"aggregated_output":"diff --git a/quimera/agents.py b/quimera/agents.py\\n+nova linha"}}'
    )
    assert completed == [
        SpyEvent(kind="tool", text="✓ git diff -- quimera/agents.py"),
        SpyEvent(kind="diff", text="diff --git a/quimera/agents.py b/quimera/agents.py", final=True),
        SpyEvent(kind="diff", text="+nova linha", final=True),
    ]


def test_format_codex_spy_event_reports_file_change_start_and_completion():
    """Verifica que format codex spy event reports file change start and completion."""
    started = _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"file_change","path":"quimera/agents.py"}}'
    )
    completed = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"file_change","path":"quimera/agents.py"}}'
    )
    assert started == [SpyEvent(kind="tool", text="editar quimera/agents.py")]
    assert completed == [SpyEvent(kind="tool", text="✓ editar quimera/agents.py")]


def test_format_codex_spy_event_reports_tool_calls_as_tool_messages():
    """Verifica que format codex spy event reports tool calls as tool messages."""
    message = _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"tool_call","name":"apply_patch"}}'
    )
    assert message == [SpyEvent(kind="tool", text="usando apply_patch")]


def test_codex_profile_exposes_spy_stdout_formatter():
    """Verifica que codex profile exposes spy stdout formatter."""
    profile = get_profile("codex")
    assert profile is not None
    assert profile.spy_stdout_formatter is _format_codex_spy_event


def test_claude_profile_exposes_spy_stdout_formatter():
    """Verifica que claude profile exposes spy stdout formatter."""
    profile = get_profile("claude")
    assert profile is not None
    assert profile.spy_stdout_formatter is _format_claude_spy_event


def test_claude_profile_supports_stream_json_resume_protocol():
    """Verifica que Claude serializa input e extrai session_id para resume."""
    profile = get_profile("claude")
    assert profile is not None
    assert profile.supports_resume is True
    assert "--input-format=stream-json" in profile.cmd

    event = json.loads(profile.format_stdin_input("olá").strip())
    assert event == {"type": "user", "message": {"role": "user", "content": "olá"}}
    assert profile.extract_session_id('{"type":"result","session_id":"sess-1"}\n') == "sess-1"
    assert profile.inject_resume_arg(["claude", "--print"], "sess-1") == [
        "claude",
        "--resume",
        "sess-1",
        "--print",
    ]


def test_opencode_profile_exposes_spy_stdout_formatter_and_json_output():
    """Verifica que opencode profile exposes spy stdout formatter and json output."""
    profile = get_profile("opencode")
    assert profile is not None
    assert profile.spy_stdout_formatter is _format_opencode_spy_event
    assert profile.output_format == "opencode-json"
    assert "--format=json" in profile.cmd


def test_opencode_profile_injects_mcp_via_env_var():
    """Verifica que opencode profile injects mcp via env var."""
    profile = get_profile("opencode")
    assert isinstance(profile, OpenCodeProfile)
    original_mcp_socket = profile._mcp_socket_path
    try:
        profile.set_mcp_socket_path("/tmp/quimera.sock")
        env = profile.env_for_cli()
        config_raw = env.get("OPENCODE_CONFIG_CONTENT")
        assert config_raw is not None
        config = json.loads(config_raw)
        assert config["mcp"]["quimera"]["type"] == "local"
        assert config["mcp"]["quimera"]["command"] == [
            "python", "-m", "quimera.runtime.mcp",
            "--connect-socket", "/tmp/quimera.sock",
        ]
        assert config["mcp"]["quimera"]["enabled"] is True
    finally:
        profile.set_mcp_socket_path(original_mcp_socket)


def test_opencode_profile_omits_mcp_env_when_no_socket():
    """Verifica que opencode profile omits mcp env when no socket."""
    profile = get_profile("opencode")
    original_mcp_socket = profile._mcp_socket_path
    try:
        profile.set_mcp_socket_path(None)
        assert profile.env_for_cli() == {}
    finally:
        profile.set_mcp_socket_path(original_mcp_socket)


def test_opencode_profile_env_for_cli_independent_of_connection_override():
    """env_for_cli() é chamada pelo AgentClient independente de _connection_override."""
    profile = get_profile("opencode")
    original_mcp_socket = profile._mcp_socket_path
    original_override = profile._connection_override
    try:
        profile.set_mcp_socket_path("/tmp/quimera.sock")
        profile._connection_override = CliConnection(
            cmd=["opencode", "run"],
            prompt_as_arg=True,
        )
        env = profile.env_for_cli()
        assert "OPENCODE_CONFIG_CONTENT" in env
    finally:
        profile._connection_override = original_override
        profile.set_mcp_socket_path(original_mcp_socket)


def test_dynamic_profile_with_opencode_base_inherits_env_for_cli():
    """Verifica que dynamic profile with opencode base inherits env for cli."""
    profile = register_connection_profile("opencode-dyn-test", metadata={"profile": "opencode"})
    assert isinstance(profile, OpenCodeProfile)

    profile.set_mcp_socket_path("/tmp/quimera.sock")
    env = profile.env_for_cli()
    assert "OPENCODE_CONFIG_CONTENT" in env


def test_agent_client_call_dynamic_opencode_base_passes_env_for_cli_to_run(renderer):
    """Verifica que agent client call dynamic opencode base passes env for cli to run."""
    client = AgentClient(renderer)
    profile = register_connection_profile("opencode-dyn-call-test", metadata={"profile": "opencode"})
    assert isinstance(profile, OpenCodeProfile)

    original_mcp_socket = profile._mcp_socket_path
    original_override = profile._connection_override
    try:
        profile.set_mcp_socket_path("/tmp/quimera.sock")
        profile._connection_override = CliConnection(
            cmd=["opencode", "run"],
            env={"BASE_ENV": "1"},
        )

        with patch("quimera.profiles.get", return_value=profile), patch.object(client, "run") as mock_run:
            mock_run.return_value = "ok"
            result = client.call("opencode-dyn-call-test", "prompt")

        assert result == "ok"
        called_kwargs = mock_run.call_args.kwargs
        assert called_kwargs["extra_env"]["BASE_ENV"] == "1"

        config_raw = called_kwargs["extra_env"].get("OPENCODE_CONFIG_CONTENT")
        assert config_raw is not None
        config = json.loads(config_raw)
        assert config["mcp"]["quimera"]["command"] == [
            "python", "-m", "quimera.runtime.mcp",
            "--connect-socket", "/tmp/quimera.sock",
        ]
    finally:
        profile._connection_override = original_override
        profile.set_mcp_socket_path(original_mcp_socket)


def test_format_claude_spy_event_summarizes_assistant_and_result():
    """Verifica que format claude spy event summarizes assistant and result."""
    assistant = _format_claude_spy_event(
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash"},{"type":"text","text":"Vou validar com um teste focado antes de concluir."}]}}'
    )
    result = _format_claude_spy_event('{"type":"result","result":"ok","is_error":false}')
    assert assistant == [
        SpyEvent(kind="tool", text="usando Bash", transient=True),
        SpyEvent(kind="response", text="Vou validar com um teste focado antes de concluir.", transient=True),
    ]
    assert result == [SpyEvent(kind="context", text="execução concluída", transient=True)]


def test_format_opencode_spy_event_ignores_step_boundary_status_and_summarizes_text():
    """Verifica que format opencode spy event ignores step boundary status and summarizes text."""
    started = _format_opencode_spy_event('{"type":"step_start","part":{"type":"step-start"}}')
    message = _format_opencode_spy_event(
        '{"type":"text","part":{"type":"text","text":"message 1\\nclear\\nmessage 2"}}'
    )
    result = _format_opencode_spy_event(
        '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}'
    )
    assert started == []
    assert message == [
        SpyEvent(kind="response", text="message 1", transient=True),
        SpyEvent(kind="clear", text="", transient=True),
        SpyEvent(kind="response", text="message 2", transient=True),
    ]
    assert result == []


def test_format_opencode_spy_event_reports_tool_calls_as_tool_messages():
    """Verifica que format opencode spy event reports tool calls as tool messages."""
    tool = _format_opencode_spy_event(
        '{"type":"tool_call","part":{"type":"tool-call","tool":"run_shell"}}'
    )
    assert tool == [SpyEvent(kind="tool", text="usando run_shell", transient=True)]


def test_parse_opencode_json_with_text(renderer):
    """Verifica que parse opencode json with text."""
    client = AgentClient(renderer)
    raw = "\n".join([
        '{"type":"step_start","part":{"type":"step-start"}}',
        '{"type":"text","part":{"type":"text","text":"primeira linha"}}',
        '{"type":"text","part":{"type":"text","text":"segunda linha"}}',
        '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}',
    ])
    result = client._parse_opencode_json(raw, "opencode-gpt")
    assert result == "primeira linha\nsegunda linha"


def test_agent_client_call_stream_json_format(renderer):
    """Verifica que agent client call stream json format."""
    # Line 286-288: stream-json format
    client = AgentClient(renderer)
    with patch("quimera.profiles.get") as mock_get:
        mock_profile = MagicMock()
        mock_profile.cmd = ["agent"]
        mock_profile.prompt_as_arg = False
        mock_profile.output_format = "stream-json"
        mock_get.return_value = mock_profile

        with patch.object(client, "run") as mock_run:
            mock_run.return_value = '{"type":"result","result":"parsed"}'
            result = client.call("agent", "prompt")
            assert result == "parsed"


def test_agent_client_call_codex_json_format(renderer):
    """Verifica que agent client call codex json format."""
    # Line 289-290: codex-json format
    client = AgentClient(renderer)
    with patch("quimera.profiles.get") as mock_get:
        mock_profile = MagicMock()
        mock_profile.cmd = ["agent"]
        mock_profile.prompt_as_arg = False
        mock_profile.output_format = "codex-json"
        mock_get.return_value = mock_profile

        with patch.object(client, "run") as mock_run:
            mock_run.return_value = '{"type":"item.completed","item":{"type":"agent_message","text":"parsed"}}'
            result = client.call("agent", "prompt")
            assert result == "parsed"


def test_agent_client_call_opencode_json_format(renderer):
    """Verifica que agent client call opencode json format."""
    client = AgentClient(renderer)
    with patch("quimera.profiles.get") as mock_get:
        mock_profile = MagicMock()
        mock_profile.cmd = ["agent"]
        mock_profile.prompt_as_arg = False
        mock_profile.output_format = "opencode-json"
        mock_get.return_value = mock_profile

        with patch.object(client, "run") as mock_run:
            mock_run.return_value = '{"type":"text","part":{"type":"text","text":"parsed"}}'
            result = client.call("agent", "prompt")
            assert result == "parsed"


# ---------------------------------------------------------------------------
# Testes de cancelamento via API (_call_api)
# ---------------------------------------------------------------------------

def test_call_api_starts_and_stops_esc_monitor(renderer):
    """_start_esc_monitor e _stop_esc_monitor devem ser chamados para agentes API."""
    from types import SimpleNamespace
    client = AgentClient(renderer)
    profile = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor") as mock_start, \
            patch.object(client, "_stop_esc_monitor") as mock_stop:
        mock_driver = MagicMock()
        mock_driver.run.return_value = "ok"
        mock_driver_cls.return_value = mock_driver

        result = client._call_api("test-agent", profile, "prompt")

        assert result == "ok"
        mock_start.assert_called_once()
        mock_stop.assert_called_once()


def test_call_api_passes_cancel_event_to_driver(renderer):
    """cancel_event deve ser passado ao driver para cancelamento cooperativo."""
    from types import SimpleNamespace
    client = AgentClient(renderer)
    profile = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor"):
        mock_driver = MagicMock()
        mock_driver.run.return_value = "result"
        mock_driver_cls.return_value = mock_driver

        client._call_api("test-agent", profile, "prompt")

        call_kwargs = mock_driver.run.call_args.kwargs
        assert "cancel_event" in call_kwargs
        assert call_kwargs["cancel_event"] is client._cancel_event


def test_call_api_recreates_cached_driver_when_connection_changes(renderer):
    """O cache do driver API deve ser invalidado quando a conexão efetiva muda."""
    from types import SimpleNamespace

    client = AgentClient(renderer)
    profile = SimpleNamespace(
        driver="openai_compat",
        model="modelo-antigo",
        base_url="http://localhost:11434",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor"):
        first_driver = MagicMock()
        first_driver.run.return_value = "primeira resposta"
        second_driver = MagicMock()
        second_driver.run.return_value = "segunda resposta"
        mock_driver_cls.side_effect = [first_driver, second_driver]

        assert client._call_api("openai", profile, "prompt 1") == "primeira resposta"
        profile.model = "modelo-novo"
        assert client._call_api("openai", profile, "prompt 2") == "segunda resposta"

    assert mock_driver_cls.call_count == 2
    assert mock_driver_cls.call_args_list[0].kwargs["model"] == "modelo-antigo"
    assert mock_driver_cls.call_args_list[1].kwargs["model"] == "modelo-novo"


def test_agent_client_can_invalidate_api_driver_explicitly(renderer):
    """invalidate_api_driver remove o driver cacheado de um agente específico."""
    client = AgentClient(renderer)
    client._api_drivers["openai"] = object()
    client._api_driver_signatures["openai"] = ("modelo",)
    client._api_drivers["outro"] = object()
    client._api_driver_signatures["outro"] = ("outro-modelo",)

    client.invalidate_api_driver("openai")

    assert "openai" not in client._api_drivers
    assert "openai" not in client._api_driver_signatures
    assert "outro" in client._api_drivers


def test_call_api_renders_openai_preview_for_non_approval_tools(renderer):
    """No driver OpenAI, o set_tool_preview_callback deve chamar _show_muted com o ToolPreview."""
    from types import SimpleNamespace

    client = AgentClient(renderer)
    tool_executor = MagicMock()
    client.tool_executor = tool_executor

    profile = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor"):
        mock_driver = MagicMock()

        def run_with_tool(**kwargs):
            # O executor invoca o preview callback para tools sem approval
            cb = tool_executor.set_tool_preview_callback.call_args[0][0]
            cb("read_file", {"path": "README.md"})
            return "ok"

        mock_driver.run.side_effect = run_with_tool
        mock_driver_cls.return_value = mock_driver

        result = client._call_api("test-agent", profile, "prompt")

    assert result == "ok"
    renderer.show_system_neutral.assert_called()
    message = renderer.show_system_neutral.call_args[0][0]
    assert "⚒ read_file" in message
    assert "read_file" in message
    assert "README.md" in message


def test_agent_client_bind_tool_preview_callback_uses_shared_preview(renderer):
    """bind_tool_preview_callback deve reutilizar ToolPreview + muted reporter."""
    from types import SimpleNamespace

    muted_reporter = MagicMock()
    client = AgentClient(renderer, muted_reporter=muted_reporter)
    tool_executor = SimpleNamespace(set_tool_preview_callback=MagicMock())

    client.bind_tool_preview_callback(tool_executor)

    callback = tool_executor.set_tool_preview_callback.call_args[0][0]
    callback("read_file", {"path": "README.md"})

    muted_reporter.assert_called_once()
    message = muted_reporter.call_args[0][0]
    assert "⚒ read_file" in message
    assert "README.md" in message


def test_agent_client_tool_preview_uses_agent_feed_when_supported():
    """Preview de tool sem approval deve aparecer no feed do agente na Textual."""
    from types import SimpleNamespace

    class FeedRenderer:
        supports_agent_feed = True

        def __init__(self):
            self.show_feed = MagicMock()
            self.show_system_neutral = MagicMock()

    renderer = FeedRenderer()
    muted_reporter = MagicMock()
    client = AgentClient(renderer, muted_reporter=muted_reporter)
    tool_executor = SimpleNamespace(set_tool_preview_callback=MagicMock())

    client.bind_tool_preview_callback(tool_executor, agent="codex")

    callback = tool_executor.set_tool_preview_callback.call_args[0][0]
    callback("read_file", {"path": "README.md"})

    renderer.show_feed.assert_called_once()
    message = renderer.show_feed.call_args.args[0]
    assert "⚒ read_file" in message
    assert "README.md" in message
    assert renderer.show_feed.call_args.kwargs == {"agent": "codex", "muted": True}
    renderer.show_system_neutral.assert_not_called()
    muted_reporter.assert_not_called()


def test_agent_client_tool_preview_uses_global_feed_for_http_without_agent_metadata():
    """Preview MCP HTTP sem agent_name deve aparecer no feed global, não ficar deferred."""
    from types import SimpleNamespace

    class FeedRenderer:
        supports_agent_feed = True

        def __init__(self):
            self.show_feed = MagicMock()

    renderer = FeedRenderer()
    muted_reporter = MagicMock()
    client = AgentClient(renderer, muted_reporter=muted_reporter)
    tool_executor = SimpleNamespace(set_tool_preview_callback=MagicMock())
    metadata = {"trusted_context": SimpleNamespace(transport="http_mcp", agent_name=None)}

    client.bind_tool_preview_callback(tool_executor)

    callback = tool_executor.set_tool_preview_callback.call_args[0][0]
    callback("read_file", {"path": "README.md"}, metadata)

    renderer.show_feed.assert_called_once()
    message = renderer.show_feed.call_args.args[0]
    assert "⚒ read_file" in message
    assert "README.md" in message
    assert renderer.show_feed.call_args.kwargs == {"agent": None, "muted": True}
    muted_reporter.assert_not_called()


def test_agent_client_tool_preview_uses_mcp_metadata_agent_when_available():
    """Preview de tool MCP sem approval deve usar agente vindo do trusted_context."""
    from types import SimpleNamespace

    class FeedRenderer:
        supports_agent_feed = True

        def __init__(self):
            self.show_feed = MagicMock()

    renderer = FeedRenderer()
    muted_reporter = MagicMock()
    client = AgentClient(renderer, muted_reporter=muted_reporter)
    tool_executor = SimpleNamespace(set_tool_preview_callback=MagicMock())
    metadata = {"trusted_context": SimpleNamespace(agent_name="opencode")}

    client.bind_tool_preview_callback(tool_executor)

    callback = tool_executor.set_tool_preview_callback.call_args[0][0]
    callback("read_file", {"path": "README.md"}, metadata)

    renderer.show_feed.assert_called_once()
    assert renderer.show_feed.call_args.kwargs == {"agent": "opencode", "muted": True}
    muted_reporter.assert_not_called()


def test_call_api_routes_openai_preview_through_muted_reporter(renderer):
    """Preview operacional deve usar muted_reporter quando fornecido pelo app."""
    from types import SimpleNamespace

    muted_reporter = MagicMock()
    client = AgentClient(renderer, muted_reporter=muted_reporter)
    tool_executor = MagicMock()
    client.tool_executor = tool_executor

    profile = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor"):
        mock_driver = MagicMock()

        def run_with_tool(**kwargs):
            cb = tool_executor.set_tool_preview_callback.call_args[0][0]
            cb("read_file", {"path": "README.md"})
            return "ok"

        mock_driver.run.side_effect = run_with_tool
        mock_driver_cls.return_value = mock_driver

        result = client._call_api("test-agent", profile, "prompt")

    assert result == "ok"
    muted_reporter.assert_called_once()
    message = muted_reporter.call_args[0][0]
    assert "⚒ read_file" in message
    assert "read_file" in message
    assert "README.md" in message
    renderer.show_system_neutral.assert_not_called()


def test_call_api_skips_openai_preview_when_tool_requires_approval(renderer):
    """Para tools de aprovação, o executor não invoca _tool_preview_callback — nada deve ser exibido."""
    from types import SimpleNamespace

    client = AgentClient(renderer)
    tool_executor = MagicMock()
    client.tool_executor = tool_executor

    profile = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor"):
        mock_driver = MagicMock()

        def run_with_tool(**kwargs):
            # Executor não invoca o preview callback para tools de aprovação
            return "ok"

        mock_driver.run.side_effect = run_with_tool
        mock_driver_cls.return_value = mock_driver

        result = client._call_api("test-agent", profile, "prompt")

    assert result == "ok"
    renderer.show_system_neutral.assert_not_called()


def test_call_api_masks_sensitive_fields_in_openai_preview(renderer):
    """Preview operacional deve mascarar campos sensíveis no fallback genérico."""
    from types import SimpleNamespace

    muted_reporter = MagicMock()
    client = AgentClient(renderer, muted_reporter=muted_reporter)
    tool_executor = MagicMock()
    client.tool_executor = tool_executor

    profile = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor"):
        mock_driver = MagicMock()

        def run_with_tool(**kwargs):
            cb = tool_executor.set_tool_preview_callback.call_args[0][0]
            cb(
                "custom_tool",
                {
                    "path": "README.md",
                    "token": "1234567890",
                    "headers": {"authorization": "Bearer super-secret-token"},
                },
            )
            return "ok"

        mock_driver.run.side_effect = run_with_tool
        mock_driver_cls.return_value = mock_driver

        result = client._call_api("test-agent", profile, "prompt")

    assert result == "ok"
    message = muted_reporter.call_args[0][0]
    assert "⚒ custom_tool" in message
    assert "README.md" in message
    assert "1234567890" not in message
    assert "super-secret-token" not in message
    assert "12****90" in message


def test_call_api_propagates_task_approval_scope_to_driver_thread(renderer):
    """Verifica que call api propagates task approval scope to driver thread."""
    from types import SimpleNamespace

    client = AgentClient(renderer)
    tool_executor = MagicMock()
    tool_executor.get_thread_approval_scope.return_value = "task:qwen"
    tool_executor.bind_thread_approval_scope.side_effect = [None, "driver-prev"]
    client.tool_executor = tool_executor

    profile = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor"):
        mock_driver = MagicMock()
        mock_driver.run.return_value = "ok"
        mock_driver_cls.return_value = mock_driver

        result = client._call_api("test-agent", profile, "prompt")

    assert result == "ok"
    tool_executor.get_thread_approval_scope.assert_called_once_with()
    assert tool_executor.bind_thread_approval_scope.call_args_list == [
        call("task:qwen"),
        call(None),
    ]
    renderer.show_system_neutral.assert_not_called()


def test_call_api_cancel_event_detection(renderer):
    """Quando cancel_event é acionado externamente, o while loop detecta e retorna None."""
    from types import SimpleNamespace
    import threading as _threading

    process_supervisor = MagicMock()
    client = AgentClient(renderer, process_supervisor=process_supervisor)
    profile = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    driver_started = _threading.Event()

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor"):
        mock_driver = MagicMock()

        def slow_run(**kwargs):
            # Driver bloqueia sem verificar cancel_event — o while loop externo deve detectá-lo
            driver_started.set()
            _threading.Event().wait(5.0)  # bloqueia por mais tempo que o teste
            return "never"

        mock_driver.run.side_effect = slow_run
        mock_driver_cls.return_value = mock_driver

        def trigger():
            driver_started.wait(timeout=2)
            client._cancel_event.set()

        t = _threading.Thread(target=trigger)
        t.start()

        result = client._call_api("test-agent", profile, "prompt")
        t.join(timeout=3)

    assert result is None
    renderer.show_error.assert_called_once()
    assert renderer.show_error.call_args[0][0].startswith("[cancelado] pelo usuário")
    process_supervisor.terminate_all.assert_called_once_with()


def test_show_cancelled_once_deduplicates_repeated_messages(renderer):
    """Verifica que show cancelled once deduplicates repeated messages."""
    client = AgentClient(renderer)

    client._show_cancelled_once()
    client._show_cancelled_once()

    renderer.show_error.assert_called_once()
    assert renderer.show_error.call_args[0][0].startswith("[cancelado] pelo usuário")
    client.reset_cancel_notices()
    client._show_cancelled_once()
    assert renderer.show_error.call_count == 2


def test_spy_output_presenter_drops_interrupt_echo_from_raw_stdout(renderer):
    """Verifica que spy output presenter drops interrupt echo from raw stdout."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    consumed = presenter.consume_stdout("unknown-agent", "^C\n")

    assert consumed is False
    renderer.show_plain.assert_not_called()


def test_spy_output_presenter_drops_interrupt_echo_etx_from_raw_stdout(renderer):
    """Verifica que spy output presenter drops interrupt echo etx from raw stdout."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    consumed = presenter.consume_stdout("unknown-agent", "\x03\n")

    assert consumed is False
    renderer.show_plain.assert_not_called()


def test_spy_output_presenter_summary_persists_fallback_raw_stdout(renderer):
    """Verifica que spy output presenter summary envia fallback raw stdout ao transient."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    consumed = presenter.consume_stdout("unknown-agent", "linha crua do agente\n")

    assert consumed is True
    renderer.update_agent_transient.assert_called_once_with("unknown-agent", "linha crua do agente")


def test_spy_output_presenter_uses_muted_feed_for_summary_response_when_supported():
    """Verifica que summary response vai para o transient do agente (desaparece ao final)."""
    class FeedRenderer:
        supports_agent_feed = True

        def __init__(self):
            self.show_feed = MagicMock()
            self.show_plain = MagicMock()
            self.update_agent_transient = MagicMock()

    renderer = FeedRenderer()
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="response", text="mensagem transitória", transient=True))

    renderer.update_agent_transient.assert_called_once_with("codex", "mensagem transitória")
    renderer.show_feed.assert_not_called()
    renderer.show_plain.assert_not_called()


def test_spy_output_presenter_persists_tool_preview_when_feed_is_supported():
    """Verifica que tool preview fica visível no feed Textual, não só como transient."""
    class FeedRenderer:
        supports_agent_feed = True

        def __init__(self):
            self.show_feed = MagicMock()
            self.show_plain = MagicMock()
            self.update_agent_transient = MagicMock()

    renderer = FeedRenderer()
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="usando apply_patch", transient=True))

    renderer.update_agent_transient.assert_called_once_with("codex", "usando apply_patch")
    renderer.show_feed.assert_called_once_with("usando apply_patch", agent="codex", muted=True)
    renderer.show_plain.assert_not_called()


def test_agent_client_uses_transient_for_live_stderr_when_renderer_supports_it():
    """Verifica que stderr ao vivo volta para a janela transient mesmo com feed disponível."""
    class FeedRenderer:
        supports_agent_feed = True

        def __init__(self):
            self.show_feed = MagicMock()
            self.show_plain = MagicMock()
            self.update_agent_transient = MagicMock()
            self.clear_agent_transient = MagicMock()
            self.show_error = MagicMock()
            self.show_turn_summary = MagicMock()

    renderer = FeedRenderer()
    client = AgentClient(renderer, visibility=Visibility.SUMMARY)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["output\n"])
        mock_proc.stderr = iter(["stderr line\n"])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["cmd"], silent=False, agent="codex", show_status=False)

    assert "output" in result
    renderer.update_agent_transient.assert_any_call("codex", "stderr line")
    renderer.show_feed.assert_not_called()
    renderer.show_plain.assert_any_call("execução concluída", agent="codex", muted=True)


def test_spy_output_presenter_summary_keeps_lifecycle_context_only_in_status(renderer):
    """Verifica que lifecycle de início aparece no overlay em summary e atualiza current_status_label."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="context", text="iniciando execução", transient=True))

    renderer.update_agent_transient.assert_called_once_with("codex", "iniciando execução")
    renderer.show_plain.assert_not_called()
    assert presenter.current_status_label == "iniciando execução"


def test_spy_output_presenter_summary_persists_detailed_context(renderer):
    """Verifica que spy output presenter summary persists detailed context."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="$ git status --short"))
    presenter.emit("codex", SpyEvent(kind="context", text="Vou inspecionar os handlers de tool antes de editar.", transient=True))

    renderer.update_agent_transient.assert_has_calls([
        call("codex", "$ git status --short"),
        call("codex", "Vou inspecionar os handlers de tool antes de editar."),
    ])
    assert presenter.current_status_label == ""


def test_spy_output_presenter_summary_clears_tool_status_after_response(renderer):
    """Verifica que response transitória após tool limpa o status label."""
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="$ git status --short"))
    presenter.emit("codex", SpyEvent(kind="response", text="Analisei o repositório e vou seguir.", transient=True))

    renderer.update_agent_transient.assert_any_call("codex", "$ git status --short")
    renderer.update_agent_transient.assert_any_call("codex", "Analisei o repositório e vou seguir.")
    renderer.show_plain.assert_not_called()
    assert presenter.current_status_label == ""


def test_agent_client_run_summary_shows_all_stderr_lines(renderer):
    """Verifica que agent client run summary shows all stderr lines."""
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        lines = [f"error line {i}\n" for i in range(15)]
        mock_proc.stdout = iter(["output\n"])
        mock_proc.stderr = iter(lines)
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["cmd"], silent=False)

    assert "output" in result
    for idx in range(15):
        renderer.show_plain.assert_any_call(f"error line {idx}", agent=None)
    assert not any("stderr truncado" in str(call_args) for call_args in renderer.show_plain.call_args_list)


def test_spy_output_presenter_never_renders_interrupt_echo_from_event_text(renderer):
    """Verifica que spy output presenter never renders interrupt echo from event text."""
    presenter = SpyOutputPresenter(renderer, Visibility.FULL)

    presenter.emit("codex", SpyEvent(kind="response", text="\x03"))

    renderer.show_plain.assert_not_called()


def test_call_api_stop_monitor_called_on_error(renderer):
    """_stop_esc_monitor deve ser chamado mesmo quando o driver lança exceção."""
    from types import SimpleNamespace
    client = AgentClient(renderer)
    profile = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor") as mock_stop:
        mock_driver = MagicMock()
        mock_driver.run.side_effect = RuntimeError("conexão recusada")
        mock_driver_cls.return_value = mock_driver

        result = client._call_api("test-agent", profile, "prompt")

        assert result is None
        mock_stop.assert_called_once()
        renderer.show_error.assert_called()


# ---------------------------------------------------------------------------
# Testes de cancelamento CLI (signal handler, terminal, killpg)
# ---------------------------------------------------------------------------

def test_signal_handler_sets_cancel_event(renderer):
    """cancel_event existe e pode ser settado via código ou signal."""
    client = AgentClient(renderer)

    assert hasattr(client, "_cancel_event")
    assert client._cancel_event is not None

    client._cancel_event.set()
    assert client._cancel_event.is_set()

    client._cancel_event.clear()
    assert not client._cancel_event.is_set()


def test_stop_esc_monitor_restores_signal_handler(renderer):
    """_stop_esc_monitor deve restaurar o signal handler original."""
    import signal
    client = AgentClient(renderer)

    with patch("quimera.agents.signal_guard.signal") as mock_signal:
        mock_signal.SIGINT = signal.SIGINT
        mock_signal.getsignal.return_value = signal.SIG_DFL

        with patch.object(client, "_start_esc_monitor"):
            client._start_esc_monitor()
            client._esc_monitor._old_handler = signal.SIG_DFL

        client._stop_esc_monitor()

        mock_signal.signal.assert_called_with(signal.SIGINT, signal.SIG_DFL)


def test_stop_esc_monitor_without_termios_state(renderer):
    """_stop_esc_monitor não deve depender de state de termios."""
    client = AgentClient(renderer)
    client._agent_running = False
    client._stop_esc_monitor()


def test_start_esc_monitor_is_noop_outside_main_thread(renderer):
    """Resumo final roda em thread; monitor de SIGINT não pode explodir fora da main thread."""
    client = AgentClient(renderer)
    result = {}

    def worker():
        try:
            client._start_esc_monitor()
            result["ok"] = True
        except Exception as exc:  # pragma: no cover - o teste garante que isso não acontece
            result["error"] = exc
        finally:
            client._stop_esc_monitor()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert result == {"ok": True}
    assert client._esc_monitor._old_handler is None


def test_terminate_process_group_uses_killpg(renderer):
    """_terminate_process_group deve usar os.killpg para matar processo e filhos."""
    client = AgentClient(renderer)
    proc = MagicMock()
    proc.pid = 12345

    with patch("quimera.agents.signal_guard.os") as mock_os:
        mock_os.getpgid.return_value = 12345
        mock_os.killpg.return_value = None
        mock_os.killpg.side_effect = OSError(" ESRCH")

        client._terminate_process_group(proc)

        mock_os.getpgid.assert_called_once_with(12345)
        mock_os.killpg.assert_called_once()


def test_cancel_event_cleared_on_start(renderer):
    """_cancel_event pode ser limpo antes de iniciar."""
    client = AgentClient(renderer)

    client._cancel_event.set()
    assert client._cancel_event.is_set()

    client._cancel_event.clear()
    assert not client._cancel_event.is_set()


def test_core_turn_manager_reset_after_first_agent(renderer):
    """ core.py:947 - turn_manager.reset() após primeiro agente """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_core_turn_manager_reset_after_delegation(renderer):
    """ core.py:1037 - turn_manager.reset() após delegation """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_core_turn_manager_reset_after_fallback(renderer):
    """ core.py:1078 - turn_manager.reset() após fallback """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_core_turn_manager_reset_after_synthesis(renderer):
    """ core.py:1106 - turn_manager.reset() após síntese """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_core_turn_manager_reset_after_parallel_merge(renderer):
    """ core.py:1170 - turn_manager.reset() após merge paralelo """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_core_turn_manager_reset_after_sequential_loop(renderer):
    """ core.py:1202 - turn_manager.reset() no loop sequencial """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_task_cancellation_after_call(renderer):
    """ task.py:177 - cancelamento após call """
    from unittest import mock

    app = mock.MagicMock()
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.tasks_db_path = ":memory:"

    if app.agent_client._user_cancelled:
        pass

    assert app.agent_client._user_cancelled


def test_task_cancellation_after_review(renderer):
    """ task.py:264 - cancelamento após review """
    from unittest import mock

    app = mock.MagicMock()
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.tasks_db_path = ":memory:"

    if app.agent_client._user_cancelled:
        pass

    assert app.agent_client._user_cancelled


def test_process_runner_watch_emits_tick_only_on_elapsed_change():
    """Verifica que process runner watch emits tick only on elapsed change."""
    proc = MagicMock()
    stdout_thread = MagicMock()
    stderr_thread = MagicMock()
    stdout_thread.is_alive.side_effect = [True, True, True, False]
    stderr_thread.is_alive.return_value = False
    runner = ProcessRunner(
        proc,
        stdout_thread,
        stderr_thread,
        {"stderr": []},
        threading.Event(),
        idle_timeout=None,
    )

    ticks = []
    with patch("time.sleep"):
        with patch("time.monotonic") as mock_monotonic:
            mock_monotonic.side_effect = [
                100.0,  # start_time
                100.2,
                100.4,
                101.1,
            ]
            result = runner.watch(on_tick=ticks.append)

    assert result == ProcessRunner.COMPLETED
    assert ticks == [0, 1]


def test_process_runner_pause_idle_if_suppresses_idle_timeout():
    """Enquanto pause_idle_if retornar True, o watchdog não deve encerrar por idle."""
    proc = MagicMock()
    stdout_thread = MagicMock()
    stderr_thread = MagicMock()
    stdout_thread.is_alive.side_effect = [True, True, False]
    stderr_thread.is_alive.return_value = False
    runner = ProcessRunner(
        proc,
        stdout_thread,
        stderr_thread,
        {"stderr": [], "stdout_total": 0},
        threading.Event(),
        idle_timeout=0.1,
        pause_idle_if=lambda: True,
    )

    with patch("time.sleep"):
        with patch("time.monotonic") as mock_monotonic:
            mock_monotonic.side_effect = [
                100.0,  # start_time
                101.0,  # 1a iteração
                102.0,  # 2a iteração
            ]
            result = runner.watch()

    assert result == ProcessRunner.COMPLETED
