"""Contratos de leitura prévia para mutações de arquivo.

Os testes exercitam as tools públicas porque o ledger é compartilhado entre
``FileTools`` e ``PatchTool`` e deve permanecer isolado por run.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from quimera.runtime.approval_broker import TrustedToolExecutionContext
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.read_ledger import LedgerVerdict, ReadLedger, reset_read_ledger
from quimera.runtime.tools.files import FileTools
from quimera.runtime.tools.patch import PatchTool
from quimera.workspace import Workspace


@pytest.fixture(autouse=True)
def _reset_global_read_ledger():
    """Impede que autorizações de leitura vazem entre testes."""
    reset_read_ledger()
    yield
    reset_read_ledger()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def config(workspace: Path) -> ToolRuntimeConfig:
    return ToolRuntimeConfig(
        workspace=Workspace(workspace),
        read_before_edit="enforce",
    )


@pytest.fixture
def file_tools(config: ToolRuntimeConfig) -> FileTools:
    return FileTools(config)


@pytest.fixture
def patch_tool(config: ToolRuntimeConfig) -> PatchTool:
    return PatchTool(config)


def _call(
    name: str,
    arguments: dict,
    *,
    run_id: str = "run-a",
    agent_name: str = "agent-a",
) -> ToolCall:
    return ToolCall(
        name=name,
        arguments=arguments,
        metadata={
            "trusted_context": TrustedToolExecutionContext(
                agent_name=agent_name,
                run_id=run_id,
            )
        },
    )


def _fallback_call(name: str, arguments: dict, *, agent_name: str) -> ToolCall:
    return ToolCall(
        name=name,
        arguments=arguments,
        metadata={"calling_agent": agent_name},
    )


def _read(
    tools: FileTools,
    path: str,
    *,
    run_id: str = "run-a",
    agent_name: str = "agent-a",
    **arguments,
):
    return tools.read_file(
        _call(
            "read_file",
            {"path": path, **arguments},
            run_id=run_id,
            agent_name=agent_name,
        )
    )


def _replace(
    tools: FileTools,
    path: str,
    old: str,
    new: str,
    *,
    run_id: str = "run-a",
    agent_name: str = "agent-a",
):
    return tools.replace_text(
        _call(
            "replace_text",
            {"path": path, "old": old, "new": new},
            run_id=run_id,
            agent_name=agent_name,
        )
    )


def _force_external_change(path: Path, content: str) -> None:
    """Altera conteúdo e garante fingerprint diferente em qualquer filesystem."""
    before = path.stat()
    path.write_text(content, encoding="utf-8")
    os.utime(
        path,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
    )


def _update_patch(path: str, old: str, new: str) -> str:
    return "\n".join(
        [
            "*** Begin Patch",
            f"*** Update File: {path}",
            "@@",
            f"-{old}",
            f"+{new}",
            "*** End Patch",
        ]
    )


def test_replace_text_requires_read_in_same_run(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("before\n", encoding="utf-8")

    result = _replace(file_tools, "sample.txt", "before", "after")

    assert result.ok is False
    assert result.data["reason"] == "not_read"
    assert "read_file" in str(result.error)
    assert target.read_text(encoding="utf-8") == "before\n"


def test_replace_text_rejects_stale_fingerprint(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("before\n", encoding="utf-8")
    assert _read(file_tools, "sample.txt").ok is True
    read_mtime = target.stat().st_mtime_ns
    _force_external_change(target, "external change\n")

    result = _replace(file_tools, "sample.txt", "external change", "agent change")

    assert result.ok is False
    assert result.data["reason"] == "stale"
    assert result.data["read_mtime_ns"] == read_mtime
    assert result.data["current_mtime_ns"] == target.stat().st_mtime_ns
    assert "mudou" in str(result.error).lower()
    assert target.read_text(encoding="utf-8") == "external change\n"


def test_successful_edit_refreshes_own_fingerprint(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("one\n", encoding="utf-8")
    assert _read(file_tools, "sample.txt").ok is True

    first = _replace(file_tools, "sample.txt", "one", "two")
    second = _replace(file_tools, "sample.txt", "two", "three")

    assert first.ok is True
    assert second.ok is True
    assert target.read_text(encoding="utf-8") == "three\n"


def test_partial_read_counts_as_read(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("first\nsecond\nthird\n", encoding="utf-8")

    read_result = _read(file_tools, "sample.txt", start_line=1, end_line=1)
    edit_result = _replace(file_tools, "sample.txt", "second", "changed")

    assert read_result.ok is True
    assert read_result.content == "first\n"
    assert edit_result.ok is True
    assert target.read_text(encoding="utf-8") == "first\nchanged\nthird\n"


def test_grep_search_does_not_count_as_read(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("needle\n", encoding="utf-8")

    grep_result = file_tools.grep_search(
        _call("grep_search", {"path": "sample.txt", "pattern": "needle"})
    )
    edit_result = _replace(file_tools, "sample.txt", "needle", "changed")

    assert grep_result.ok is True
    assert edit_result.ok is False
    assert edit_result.data["reason"] == "not_read"
    assert target.read_text(encoding="utf-8") == "needle\n"


def test_write_create_is_exempt_and_registers_read(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    create = file_tools.write_file(
        _call(
            "write_file",
            {"path": "sample.txt", "content": "one", "mode": "create"},
        )
    )
    append = file_tools.write_file(
        _call(
            "write_file",
            {"path": "sample.txt", "content": " two", "mode": "append"},
        )
    )

    assert create.ok is True
    assert append.ok is True
    assert target.read_text(encoding="utf-8") == "one two"


def test_append_to_unread_existing_file_requires_read(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("one", encoding="utf-8")
    arguments = {"path": "sample.txt", "content": " two", "mode": "append"}

    blocked = file_tools.write_file(_call("write_file", arguments))
    assert blocked.ok is False
    assert blocked.data["reason"] == "not_read"
    assert target.read_text(encoding="utf-8") == "one"

    assert _read(file_tools, "sample.txt").ok is True
    allowed = file_tools.write_file(_call("write_file", arguments))
    assert allowed.ok is True
    assert target.read_text(encoding="utf-8") == "one two"


def test_append_creating_new_file_is_exempt(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "novo.txt"
    result = file_tools.write_file(
        _call(
            "write_file",
            {"path": "novo.txt", "content": "one", "mode": "append"},
        )
    )

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "one"


def test_write_file_rejects_unknown_mode(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("one", encoding="utf-8")

    result = file_tools.write_file(
        _call(
            "write_file",
            {"path": "sample.txt", "content": "two", "mode": "w"},
        )
    )

    assert result.ok is False
    assert "mode inválido" in result.error
    assert target.read_text(encoding="utf-8") == "one"


def test_write_overwrite_existing_file_requires_fresh_read(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("old", encoding="utf-8")
    arguments = {
        "path": "sample.txt",
        "content": "new",
        "mode": "overwrite",
        "replace_existing": True,
    }

    blocked = file_tools.write_file(_call("write_file", arguments))
    assert blocked.ok is False
    assert blocked.data["reason"] == "not_read"
    assert target.read_text(encoding="utf-8") == "old"

    assert _read(file_tools, "sample.txt").ok is True
    allowed = file_tools.write_file(_call("write_file", arguments))
    assert allowed.ok is True
    assert target.read_text(encoding="utf-8") == "new"


def test_remove_dry_run_is_free_but_real_remove_requires_read(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("content", encoding="utf-8")

    preview = file_tools.remove_file(
        _call("remove_file", {"path": "sample.txt", "dry_run": True})
    )
    blocked = file_tools.remove_file(
        _call("remove_file", {"path": "sample.txt", "dry_run": False})
    )

    assert preview.ok is True
    assert target.exists()
    assert blocked.ok is False
    assert blocked.data["reason"] == "not_read"
    assert target.exists()

    assert _read(file_tools, "sample.txt").ok is True
    removed = file_tools.remove_file(
        _call("remove_file", {"path": "sample.txt", "dry_run": False})
    )
    assert removed.ok is True
    assert not target.exists()


def test_remove_forgets_ledger_entry_after_success(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("first", encoding="utf-8")
    assert _read(file_tools, "sample.txt").ok is True
    assert file_tools.remove_file(
        _call("remove_file", {"path": "sample.txt", "dry_run": False})
    ).ok is True

    target.write_text("recreated", encoding="utf-8")
    result = file_tools.remove_file(
        _call("remove_file", {"path": "sample.txt", "dry_run": False})
    )

    assert result.ok is False
    assert result.data["reason"] == "not_read"
    assert target.exists()


def test_apply_patch_prechecks_all_operations_before_writing(
    file_tools: FileTools,
    patch_tool: PatchTool,
    workspace: Path,
) -> None:
    target = workspace / "existing.txt"
    target.write_text("old\n", encoding="utf-8")
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: added.txt",
            "+added",
            "*** Update File: existing.txt",
            "@@",
            "-old",
            "+new",
            "*** End Patch",
        ]
    )

    blocked = patch_tool.apply_patch(_call("apply_patch", {"patch": patch}))

    assert blocked.ok is False
    assert blocked.data["reason"] == "not_read"
    assert not (workspace / "added.txt").exists()
    assert target.read_text(encoding="utf-8") == "old\n"

    assert _read(file_tools, "existing.txt").ok is True
    allowed = patch_tool.apply_patch(_call("apply_patch", {"patch": patch}))
    assert allowed.ok is True
    assert (workspace / "added.txt").read_text(encoding="utf-8") == "added\n"
    assert target.read_text(encoding="utf-8") == "new\n"


def test_apply_patch_add_file_is_exempt(
    patch_tool: PatchTool,
    workspace: Path,
) -> None:
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: added.txt",
            "+content",
            "*** End Patch",
        ]
    )

    result = patch_tool.apply_patch(_call("apply_patch", {"patch": patch}))

    assert result.ok is True
    assert (workspace / "added.txt").read_text(encoding="utf-8") == "content\n"


def test_apply_patch_dry_run_still_requires_read(
    patch_tool: PatchTool,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("old\n", encoding="utf-8")

    result = patch_tool.apply_patch(
        _call(
            "apply_patch",
            {"patch": _update_patch("sample.txt", "old", "new"), "dry_run": True},
        )
    )

    assert result.ok is False
    assert result.data["reason"] == "not_read"
    assert target.read_text(encoding="utf-8") == "old\n"


def test_apply_patch_delete_requires_read_and_forgets_entry(
    file_tools: FileTools,
    patch_tool: PatchTool,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("content\n", encoding="utf-8")
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Delete File: sample.txt",
            "*** End Patch",
        ]
    )

    blocked = patch_tool.apply_patch(_call("apply_patch", {"patch": patch}))
    assert blocked.ok is False
    assert blocked.data["reason"] == "not_read"
    assert target.exists()


def test_apply_patch_move_requires_read_of_existing_destination(
    file_tools: FileTools,
    patch_tool: PatchTool,
    workspace: Path,
) -> None:
    source = workspace / "source.txt"
    destination = workspace / "destination.txt"
    source.write_text("old\n", encoding="utf-8")
    destination.write_text("occupied\n", encoding="utf-8")
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: source.txt",
            "*** Move to: destination.txt",
            "@@",
            "-old",
            "+new",
            "*** End Patch",
        ]
    )

    assert _read(file_tools, "source.txt").ok is True
    blocked = patch_tool.apply_patch(_call("apply_patch", {"patch": patch}))

    assert blocked.ok is False
    assert blocked.data["reason"] == "not_read"
    assert blocked.data["path"] == str(destination)
    assert source.read_text(encoding="utf-8") == "old\n"
    assert destination.read_text(encoding="utf-8") == "occupied\n"

    assert _read(file_tools, "destination.txt").ok is True
    allowed = patch_tool.apply_patch(_call("apply_patch", {"patch": patch}))
    assert allowed.ok is True
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "new\n"


def test_read_authorization_is_isolated_between_run_ids(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("old\n", encoding="utf-8")
    assert _read(file_tools, "sample.txt", run_id="run-a").ok is True

    result = _replace(
        file_tools,
        "sample.txt",
        "old",
        "new",
        run_id="run-b",
        agent_name="agent-a",
    )

    assert result.ok is False
    assert result.data["reason"] == "not_read"
    assert target.read_text(encoding="utf-8") == "old\n"


def test_concurrent_agent_edit_makes_first_agents_read_stale(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("initial\n", encoding="utf-8")
    assert _read(
        file_tools,
        "sample.txt",
        run_id="run-a",
        agent_name="agent-a",
    ).ok is True
    assert _read(
        file_tools,
        "sample.txt",
        run_id="run-b",
        agent_name="agent-b",
    ).ok is True

    agent_b = _replace(
        file_tools,
        "sample.txt",
        "initial",
        "agent b",
        run_id="run-b",
        agent_name="agent-b",
    )
    agent_a = _replace(
        file_tools,
        "sample.txt",
        "agent b",
        "agent a",
        run_id="run-a",
        agent_name="agent-a",
    )

    assert agent_b.ok is True
    assert agent_a.ok is False
    assert agent_a.data["reason"] == "stale"
    assert target.read_text(encoding="utf-8") == "agent b\n"


def test_warn_mode_allows_edit_and_reports_warning(workspace: Path) -> None:
    target = workspace / "sample.txt"
    target.write_text("old\n", encoding="utf-8")
    tools = FileTools(
        ToolRuntimeConfig(
            workspace=Workspace(workspace),
            read_before_edit="warn",
        )
    )

    result = _replace(tools, "sample.txt", "old", "new")

    assert result.ok is True
    assert "aviso" in result.content.lower()
    assert "read_file" in result.content
    assert target.read_text(encoding="utf-8") == "new\n"


def test_off_mode_allows_edit_without_read_or_warning(workspace: Path) -> None:
    target = workspace / "sample.txt"
    target.write_text("old\n", encoding="utf-8")
    tools = FileTools(
        ToolRuntimeConfig(
            workspace=Workspace(workspace),
            read_before_edit="off",
        )
    )

    result = _replace(tools, "sample.txt", "old", "new")

    assert result.ok is True
    assert "aviso" not in result.content.lower()
    assert target.read_text(encoding="utf-8") == "new\n"


def test_fallback_scope_uses_calling_agent(
    file_tools: FileTools,
    workspace: Path,
) -> None:
    target = workspace / "sample.txt"
    target.write_text("old\n", encoding="utf-8")
    read = file_tools.read_file(
        _fallback_call("read_file", {"path": "sample.txt"}, agent_name="fallback-a")
    )
    assert read.ok is True

    other_agent = file_tools.replace_text(
        _fallback_call(
            "replace_text",
            {"path": "sample.txt", "old": "old", "new": "wrong"},
            agent_name="fallback-b",
        )
    )
    same_agent = file_tools.replace_text(
        _fallback_call(
            "replace_text",
            {"path": "sample.txt", "old": "old", "new": "new"},
            agent_name="fallback-a",
        )
    )

    assert other_agent.ok is False
    assert other_agent.data["reason"] == "not_read"
    assert same_agent.ok is True
    assert target.read_text(encoding="utf-8") == "new\n"


def test_ledger_evicts_least_recently_used_scope(tmp_path: Path) -> None:
    ledger = ReadLedger(max_scopes=2, max_paths_per_scope=2)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    third = tmp_path / "third.txt"
    for path in (first, second, third):
        path.write_text(path.stem, encoding="utf-8")

    ledger.record("scope-a", first)
    ledger.record("scope-b", second)
    ledger.record("scope-c", third)

    assert ledger.scope_count == 2
    assert ledger.check("scope-a", first) is LedgerVerdict.NOT_READ
    assert ledger.check("scope-b", second) is LedgerVerdict.OK
    assert ledger.check("scope-c", third) is LedgerVerdict.OK


def test_ledger_evicts_least_recently_used_path(tmp_path: Path) -> None:
    ledger = ReadLedger(max_scopes=2, max_paths_per_scope=2)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    third = tmp_path / "third.txt"
    for path in (first, second, third):
        path.write_text(path.stem, encoding="utf-8")

    ledger.record("scope-a", first)
    ledger.record("scope-a", second)
    ledger.record("scope-a", third)

    assert ledger.path_count("scope-a") == 2
    assert ledger.get_entry("scope-a", first) is None
    assert ledger.check("scope-a", second) is LedgerVerdict.OK
    assert ledger.check("scope-a", third) is LedgerVerdict.OK
