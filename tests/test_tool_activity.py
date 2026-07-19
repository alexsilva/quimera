from quimera.domain.tool_activity import (
    ToolActivityCategory,
    classify_command_activity,
    classify_tool_activity,
    count_tool_activities,
)


def test_classifies_named_tools_without_ui_text_parsing():
    assert classify_tool_activity("read_file") is ToolActivityCategory.INSPECTION
    assert classify_tool_activity("apply_patch") is ToolActivityCategory.MODIFICATION
    assert classify_tool_activity("git_commit") is ToolActivityCategory.VERSION_CONTROL
    assert classify_tool_activity("web_search") is ToolActivityCategory.RESEARCH


def test_classifies_validation_commands_from_executable_contract():
    assert classify_command_activity("pytest -q tests/test_chat.py") is ToolActivityCategory.VALIDATION
    assert classify_command_activity("python -m pytest -q") is ToolActivityCategory.VALIDATION
    assert classify_command_activity("npm run lint") is ToolActivityCategory.VALIDATION
    assert classify_command_activity("cargo check") is ToolActivityCategory.VALIDATION


def test_classifies_git_and_shell_commands_conservatively():
    assert classify_command_activity("git diff --stat") is ToolActivityCategory.INSPECTION
    assert classify_command_activity("git commit -m 'fix'") is ToolActivityCategory.VERSION_CONTROL
    assert classify_command_activity("rm -f tmp.txt") is ToolActivityCategory.MODIFICATION
    assert classify_command_activity("python script.py") is ToolActivityCategory.EXECUTION


def test_explicit_activity_metadata_has_priority():
    assert classify_tool_activity(
        "exec_command",
        {"cmd": "python script.py"},
        explicit="validation",
    ) is ToolActivityCategory.VALIDATION


def test_count_tool_activities_supports_structured_and_legacy_records():
    counts = count_tool_activities(
        [
            {"tool": "read_file", "activity": "inspection"},
            {"tool": "apply_patch", "activity": "modification"},
            {"tool": "exec_command", "input": {"cmd": "pytest -q"}},
            {"tool": "git_commit"},
        ]
    )

    assert counts == {
        "inspection": 1,
        "modification": 1,
        "validation": 1,
        "version_control": 1,
    }
