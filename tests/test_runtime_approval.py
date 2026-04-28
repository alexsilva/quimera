from unittest.mock import patch

import pytest

from quimera.runtime.approval import ApprovalHandler, ConsoleApprovalHandler


def test_approval_handler_abstract():
    class ConcreteHandler(ApprovalHandler):
        def approve(self, *, tool_name: str, summary: str) -> bool:
            return super().approve(tool_name=tool_name, summary=summary)

    handler = ConcreteHandler()
    with pytest.raises(NotImplementedError):
        handler.approve(tool_name="test", summary="test")


@patch('builtins.input')
@patch('builtins.print')
def test_console_approval_handler_yes(mock_print, mock_input):
    handler = ConsoleApprovalHandler()
    mock_input.return_value = "y"
    assert handler.approve(tool_name="shell", summary="ls") is True
    mock_print.assert_called()


@patch('builtins.input')
def test_console_approval_handler_no(mock_input):
    handler = ConsoleApprovalHandler()
    mock_input.return_value = "n"
    assert handler.approve(tool_name="shell", summary="rm -rf /") is False


@patch('builtins.input')
def test_console_approval_handler_empty(mock_input):
    handler = ConsoleApprovalHandler()
    mock_input.return_value = ""
    assert handler.approve(tool_name="shell", summary="echo") is False


def test_console_approval_handler_with_custom_input_fn():
    """Quando input_fn é injetada, deve usá-la em vez de input() builtin."""
    calls = []

    def fake_input(prompt):
        calls.append(prompt)
        return "yes"

    handler = ConsoleApprovalHandler(input_fn=fake_input)
    result = handler.approve(tool_name="shell", summary="ls")

    assert result is True
    assert len(calls) == 1
    assert "Executar?" in calls[0]
    assert "y/N" in calls[0]
