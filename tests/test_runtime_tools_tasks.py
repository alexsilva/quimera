import os
from pathlib import Path
from unittest.mock import patch

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.tasks import TaskTools


@pytest.fixture
def config():
    return ToolRuntimeConfig(workspace_root=Path("/tmp"), db_path=Path("/tmp/tasks.db"))


@pytest.fixture
def tools(config):
    return TaskTools(config)


def test_resolve_job_id_env(tools):
    # Line 27-28 coverage
    with patch.dict(os.environ, {"QUIMERA_CURRENT_JOB_ID": "123"}):
        assert tools._resolve_job_id(None) == 123

    with patch.dict(os.environ, {"QUIMERA_CURRENT_JOB_ID": "invalid"}):
        assert tools._resolve_job_id(None) is None


@patch("quimera.runtime.tools.tasks._list_jobs")
def test_resolve_job_id_fallback(mock_list, tools):
    # Line 30-37 coverage
    # Clear environment to ensure fallback is tested
    with patch.dict(os.environ, {}, clear=True):
        mock_list.side_effect = [[{"id": 10}], [{"id": 20}]]
        assert tools._resolve_job_id(None, allow_recent_fallback=True) == 10

        mock_list.side_effect = [[], [{"id": 20}]]
        assert tools._resolve_job_id(None, allow_recent_fallback=True) == 20

        mock_list.side_effect = Exception("db error")
        assert tools._resolve_job_id(None, allow_recent_fallback=True) is None


def test_find_duplicate_task(tools):
    # Line 53-62 coverage
    with patch("quimera.runtime.tools.tasks._list_tasks") as mock_list:
        mock_list.return_value = [{"description": "  TEST description  "}]
        assert tools._find_duplicate_task(1, "test description") is not None
        assert tools._find_duplicate_task(1, "") is None

        mock_list.return_value = []
        assert tools._find_duplicate_task(1, "unique") is None


@patch("quimera.runtime.tools.tasks._list_tasks")
def test_list_tasks_error(mock_list, tools):
    # Line 69-70 coverage
    mock_list.side_effect = Exception("Boom")
    call = ToolCall(name="list_tasks", arguments={})
    result = tools.list_tasks(call)
    assert result.ok is False
    assert "Boom" in result.error


@patch("quimera.runtime.tools.tasks._list_jobs")
def test_list_jobs_error(mock_list, tools):
    # Line 81-82 coverage
    mock_list.side_effect = Exception("Boom")
    call = ToolCall(name="list_jobs", arguments={})
    result = tools.list_jobs(call)
    assert result.ok is False
    assert "Boom" in result.error


@patch("quimera.runtime.tools.tasks._get_job")
def test_get_job_no_id(mock_get, tools):
    # Line 87 coverage
    call = ToolCall(name="get_job", arguments={})
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(tools, "_resolve_job_id", return_value=None):
            result = tools.get_job(call)
            assert result.ok is False
            assert "job_id is required" in result.error


@patch("quimera.runtime.tools.tasks._get_job")
def test_get_job_error(mock_get, tools):
    # Line 91-92 coverage
    mock_get.side_effect = Exception("Boom")
    call = ToolCall(name="get_job", arguments={"job_id": 1})
    result = tools.get_job(call)
    assert result.ok is False
    assert "Boom" in result.error


def test_get_job_null(tools):
    with patch("quimera.runtime.tools.tasks._get_job") as mock_get:
        mock_get.return_value = None
        call = ToolCall(name="get_job", arguments={"job_id": 1})
        result = tools.get_job(call)
        assert result.ok is True
        assert result.content == "null"
