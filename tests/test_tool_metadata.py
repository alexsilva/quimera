"""Invariantes dos metadados canônicos de tools."""
from __future__ import annotations

from pathlib import Path

from quimera.modes import MODES
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.drivers.tool_catalog import TOOL_SPECS
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicy
from quimera.runtime.tool_metadata import (
    TOOL_METADATA,
    ApprovalMode,
    ToolRisk,
    tools_for_http_profile,
)


def test_metadata_covers_every_native_tool_exactly_once():
    """Tool nativa nova deve declarar metadata antes de entrar no catálogo."""
    catalog_names = {spec.name for spec in TOOL_SPECS}

    assert set(TOOL_METADATA) == catalog_names


def test_mutation_approval_tools_are_marked_as_mutating():
    """Approval de mutação não pode divergir do efeito semântico da tool."""
    for name, metadata in TOOL_METADATA.items():
        if metadata.approval in {
            ApprovalMode.MUTATION,
            ApprovalMode.TASK_CREATION,
            ApprovalMode.HTTP_METHOD,
        }:
            assert metadata.mutates is True, name


def test_destructive_and_shell_tools_have_explicit_risk():
    assert TOOL_METADATA["remove_file"].risk == ToolRisk.DESTRUCTIVE
    assert TOOL_METADATA["memory_delete"].risk == ToolRisk.DESTRUCTIVE
    assert TOOL_METADATA["run_shell"].risk == ToolRisk.SHELL
    assert TOOL_METADATA["exec_command"].risk == ToolRisk.SHELL


def test_http_profiles_are_derived_from_metadata():
    from quimera.runtime.mcp.http_server import (
        HTTP_AGENT_TOOLS,
        HTTP_READ_LOCAL_TOOLS,
        HTTP_READ_TOOLS,
    )

    assert HTTP_READ_LOCAL_TOOLS == tools_for_http_profile("read-local")
    assert HTTP_READ_TOOLS == tools_for_http_profile("read")
    assert HTTP_AGENT_TOOLS == tools_for_http_profile("agent")
    assert "git_fetch" not in HTTP_READ_TOOLS
    assert "git_fetch" in HTTP_AGENT_TOOLS
    assert {"host_processes", "host_process_inspect", "host_memory"} <= HTTP_READ_LOCAL_TOOLS


def test_unknown_tool_requires_approval_fail_closed(tmp_path: Path):
    policy = ToolPolicy(
        ToolRuntimeConfig(
            workspace_root=tmp_path,
            require_approval_for_mutations=True,
        )
    )

    assert policy.requires_approval(
        ToolCall(name="future_native_tool", arguments={})
    ) is True


def test_capability_metadata_preserves_multi_requirement_for_tasks():
    assert TOOL_METADATA["tasks"].capabilities == ("task_db", "tasks")
    assert TOOL_METADATA["list_tasks"].capabilities == ("task_db",)
    assert TOOL_METADATA["delegate"].capabilities == ("delegate",)


def test_read_only_modes_fail_closed_for_mutating_tools():
    mutation_names = {
        name for name, metadata in TOOL_METADATA.items() if metadata.mutates
    }
    interactive_shell = {
        "run_shell",
        "exec_command",
        "write_stdin",
        "poll_command_session",
        "close_command_session",
    }

    for mode_name in ("/planning", "/analysis"):
        blocked = set(MODES[mode_name].blocked_tools)
        assert mutation_names - interactive_shell <= blocked
        assert blocked.isdisjoint(interactive_shell)

    for mode_name in ("/design", "/review"):
        assert mutation_names <= set(MODES[mode_name].blocked_tools)
