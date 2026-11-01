"""Contrato da linha estruturada de tool (quimera/domain/tool_line.py)."""
from __future__ import annotations

import unicodedata
from typing import Any

import pytest
from rich._unicode_data import VERSIONS as RICH_VERSIONS
from rich.cells import cell_len, get_character_cell_size

from quimera.domain.tool_activity import (
    COMMAND_TOOLS,
    DELEGATION_TOOLS,
    INSPECTION_TOOLS,
    MODIFICATION_TOOLS,
    RESEARCH_TOOLS,
    VERSION_CONTROL_TOOLS,
    ToolActivityCategory,
    classify_tool_activity,
    unwrap_shell_command,
)
from quimera.domain.tool_line import (
    CATEGORY_GLYPHS,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_RUNNING,
    ToolLine,
    build_tool_line,
    coerce_duration_ms,
    is_sensitive_key,
    tool_call_namespace,
)
from quimera.ui.textual.cell_widths import EMOJI_NARROW_VERSION


# --- to_text: exemplos do contrato ------------------------------------------------

@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (build_tool_line("Bash", {"command": "grep -n tool x.py", "description": "Locate tool code"}),
         "⌕ grep -n tool x.py"),
        (build_tool_line("Bash", {"command": "/bin/bash -lc 'pytest tests/x.py'"},
                         status="error", exit_code=1, duration_ms=12400),
         "✗ ⚙ pytest tests/x.py · 12.4s · exit 1"),
        (build_tool_line("exec_command", {"cmd": "git status"}, status="ok", duration_ms=300),
         "✓ ⌕ git status · 300ms"),
        (build_tool_line("read_file", {"path": "quimera/app.py"}), "⌕ ler quimera/app.py"),
        (build_tool_line("mcp__quimera__tasks_delegate",
                         {"target_agent": "codex-gpt-5-6", "request": "Revise o módulo X\n..."}),
         "⇄ delegar codex-gpt-5-6 · Revise o módulo X"),
        (build_tool_line("Edit", {"file_path": "a.py"}, status="ok", duration_ms=50), "✓ ✎ editar a.py · 50ms"),
        (build_tool_line("python_tool", {"token": "abcdef", "path": "x"}), "$ python_tool x ab****ef"),
        (build_tool_line("ToolSearch", {"query": "select:mcp__quimera__tui_screenshot", "max_results": 1},
                         status="ok", duration_ms=202),
         "✓ $ toolsearch select:mcp__quimera__tui_screenshot 1 · 202ms"),
        (build_tool_line("exec_command", {"cmd": "python3 script.py"}), "$ python3 script.py"),
        (build_tool_line("read_file", {"path": "a"}, transport="mcp_http", status="ok", duration_ms=10),
         "◇ ✓ ⌕ ler a · 10ms"),
    ],
)
def test_to_text_contract_examples(line: ToolLine, expected: str) -> None:
    assert line.to_text() == expected


def test_python_tool_is_execution_and_hides_token() -> None:
    line = build_tool_line("python_tool", {"token": "abcdef", "path": "x"})
    assert line.category == "execution"
    assert "abcdef" not in line.to_text()


def test_running_line_omits_duration_and_validation_shows_tail() -> None:
    running = build_tool_line("Bash", {"command": "pytest -q"}, duration_ms=10, tail="3 passed")
    assert running.to_text() == "⚙ pytest -q · 3 passed"
    inspection = build_tool_line("read_file", {"path": "a"}, status="ok", tail="conteúdo")
    assert inspection.to_text() == "✓ ⌕ ler a"


# --- verb e subject por família ---------------------------------------------------

@pytest.mark.parametrize(
    ("tool", "args", "verb", "subject"),
    [
        ("bash", {"command": "ls -la"}, "", "ls -la"),
        ("run", {"input": "make"}, "", "make"),
        ("shell", {"cmd": "echo  a\n  b"}, "", "echo a b"),
        ("Read", {"file_path": "a.py"}, "ler", "a.py"),
        ("view", {"filename": "b.py"}, "ler", "b.py"),
        ("Write", {"file_path": "c.py", "content": "x"}, "escrever", "c.py"),
        ("write_file", {"path": "c.py"}, "escrever", "c.py"),
        ("MultiEdit", {"file_path": "d.py"}, "editar", "d.py"),
        ("NotebookEdit", {"notebook_path": "n.ipynb"}, "editar", "n.ipynb"),
        ("apply_patch", {"patch": "--- a\n+++ b"}, "editar", ""),
        ("remove_file", {"path": "old.py"}, "remover", "old.py"),
        ("Grep", {"pattern": "foo", "path": "src"}, "buscar", '"foo" em src'),
        ("grep_search", {"pattern": "bar"}, "buscar", '"bar"'),
        ("rg", {"pattern": "x", "root": "."}, "buscar", '"x" em .'),
        ("Glob", {"pattern": "**/*.py"}, "listar", "**/*.py"),
        ("list_files", {"directory": "quimera"}, "listar", "quimera"),
        ("WebSearch", {"query": "rich cells"}, "pesquisar", '"rich cells"'),
        ("web_search", {"q": "x"}, "pesquisar", '"x"'),
        ("WebFetch", {"url": "https://a.b"}, "abrir", "https://a.b"),
        ("browser_navigate", {"uri": "https://c.d"}, "abrir", "https://c.d"),
        ("git_status", {}, "git", "status"),
        ("git_add", {"paths": ["quimera/x.py", "tests/y.py"]}, "git", "add quimera/x.py tests/y.py"),
        ("git_commit", {"message": "mensagem\n\ncorpo"}, "git", 'commit "mensagem"'),
        ("git_checkout", {"branch": "dev"}, "git", "checkout dev"),
        ("git_push", {"remote": "origin", "branch": "dev"}, "git", "push origin dev"),
        ("git_diff", {"path": "a.py"}, "git", "diff a.py"),
        ("tasks_delegate", {"target_agent": "claude-opus"}, "delegar", "claude-opus"),
        ("tasks_create", {"description": "Revisar X"}, "delegar", "Revisar X"),
        ("workflows_start", {"workflow_id": "wf-1"}, "delegar", "wf-1"),
        ("Task", {"description": "Explorar", "prompt": "longo"}, "delegar", "Explorar"),
        ("agent", {"subagent_type": "Explore"}, "delegar", "Explore"),
        ("write_stdin", {"session_id": "s", "chars": "y"}, "", "session=s 'y'"),
        ("close_command_session", {"session_id": "s"}, "", "session=s"),
        ("ask_user", {"question": "Seguir?"}, "perguntar", "Seguir?"),
    ],
)
def test_verb_and_subject_by_family(tool: str, args: dict, verb: str, subject: str) -> None:
    line = build_tool_line(tool, args)
    assert (line.verb, line.subject) == (verb, subject)


def test_delegation_request_first_line_is_capped() -> None:
    line = build_tool_line("tasks_delegate", {"target_agent": "a", "request": "x" * 200})
    assert line.subject == "a · " + "x" * 80 + "…"


def test_ask_user_question_is_capped() -> None:
    assert build_tool_line("ask_user", {"question": "q" * 300}).subject == "q" * 120 + "…"


def test_missing_family_field_falls_back_to_unknown_rule() -> None:
    line = build_tool_line("read_file", {"name": "x", "limit": 3})
    assert (line.verb, line.subject) == ("read_file", "x 3")
    assert build_tool_line("bash", {}).subject == ""


def test_unknown_rule_prefers_known_keys_and_redacts() -> None:
    assert build_tool_line("custom", {"limit": 3, "query": "q"}).subject == "q 3"
    assert build_tool_line("custom", {"a": 1, "b": 2, "c": 3, "query": "q"}).subject == "q 1 2 +1 args"
    assert build_tool_line("custom", {"password": "hunter22"}).subject == "hu****22"
    # Variações de caixa, hífen e prefixo também são mascaradas.
    leaky = build_tool_line(
        "tasks_list", {"query": "x", "access_token": "abcdefghij", "API-Key": "zzzzzzzz", "Client_Secret": "s3cr3tvalue"}
    )
    assert leaky.subject == "x ab****ij zz****zz +1 args"
    assert "s3cr3tvalue" not in leaky.to_text()


def test_sensitive_key_matching_does_not_hide_ordinary_keys() -> None:
    for key in (
        "access_token", "Refresh-Token", "x-api-key", "apikey", "private_key", "passwd", "Bearer", "headers",
        "accessToken", "clientSecret", "authToken", "apiKey", "secrets", "cookies", "access_key", "passphrase",
        "pwd", "jwt", "dsn", "databaseUrl", "connection_string",
        "signingKey", "encryption_key", "ssh_key", "client_key", "password1",
    ):
        assert is_sensitive_key(key), key
    for key in ("max_tokens", "maxTokens", "query", "keyword", "path", "monkey", "limit", "tokenizer", "author", "session_id",
                "sort_key", "cache_key"):
        assert not is_sensitive_key(key), key


def test_unknown_values_are_clipped_one_by_one() -> None:
    subject = build_tool_line("custom", {"query": "a\n" * 300, "limit": 3, "x": 1, "y": 2}).subject
    first, rest = subject.split("…", 1)
    assert len(first) == 80
    assert rest == " 3 1 +1 args"
    assert build_tool_line("custom", {"nested": {"a": 1}}).subject == "+1 args"
    assert build_tool_line("custom", {}).to_text() == "$ custom"


def test_subject_has_protection_cap() -> None:
    line = build_tool_line("bash", {"command": "echo " + "x" * 1000})
    assert len(line.subject) == 401
    assert line.subject.endswith("…")


# --- invólucro do shell -----------------------------------------------------------

@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/bin/bash -lc 'pytest tests/x.py'", "pytest tests/x.py"),
        ('bash -c "ls -la && pwd"', "ls -la && pwd"),
        ("git status --short", "git status --short"),
        ("bash -lc 'ls' extra", "bash -lc 'ls' extra"),
        ("bash -lc 'unterminated", "bash -lc 'unterminated"),
        ("bash --login 'x'", "bash --login 'x'"),
    ],
)
def test_unwrap_shell_command(command: str, expected: str) -> None:
    assert unwrap_shell_command(command) == expected
    assert build_tool_line("exec_command", {"cmd": command}).subject == expected


# --- status, tail, duração --------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", STATUS_RUNNING), (None, STATUS_RUNNING), ("running", STATUS_RUNNING),
        ("ok", STATUS_OK), ("success", STATUS_OK), ("Completed", STATUS_OK), ("done", STATUS_OK),
        ("error", STATUS_ERROR), ("failed", STATUS_ERROR), ("timeout", STATUS_ERROR),
        ("cancelled", STATUS_ERROR), ("aborted", STATUS_ERROR),
    ],
)
def test_status_normalization(raw: Any, expected: str) -> None:
    assert build_tool_line("bash", {"command": "ls"}, status=raw).status == expected


def test_tail_sources_in_priority_order() -> None:
    assert build_tool_line("bash", tail="a\nfinal\n\n", error="e", exit_code=2).tail == "final"
    assert build_tool_line("bash", error={"message": "boom\n  detalhe"}, exit_code=2).tail == "boom detalhe"
    assert build_tool_line("bash", error="falhou", exit_code=2).tail == "falhou"
    assert build_tool_line("bash", exit_code=2).tail == "exit 2"
    assert build_tool_line("bash", exit_code=0).tail == ""
    assert build_tool_line("bash").tail == ""
    assert build_tool_line("bash", tail="t" * 300).tail == "t" * 160 + "…"


@pytest.mark.parametrize("raw", [-1, True, 1.5, "10", None])
def test_invalid_duration_becomes_none(raw: Any) -> None:
    assert build_tool_line("bash", duration_ms=raw).duration_ms is None


def test_title_is_single_line_and_capped() -> None:
    line = build_tool_line("bash", {"command": "ls"}, description="a\n b" + "c" * 300)
    assert line.title.startswith("a bc")
    assert len(line.title) == 161


def test_description_argument_is_title_only_for_shell() -> None:
    assert build_tool_line("Bash", {"command": "ls", "description": "Listar"}).title == "Listar"
    assert build_tool_line("Task", {"description": "Explorar"}).title == ""


def test_identity_fields() -> None:
    line = build_tool_line(
        "mcp__quimera__read_file", {"path": "a"}, tool_call_id=7, transport="mcp_http",
        explicit_category="validation", preview_lines=["+x", "-y"],
    )
    assert line.tool == "read_file"
    assert line.tool_call_id == "7"
    assert line.category == "validation"
    assert line.preview_lines == ("+x", "-y")
    assert build_tool_line("read_file").tool_call_id == ""


# --- payload ----------------------------------------------------------------------

def test_payload_roundtrip() -> None:
    line = build_tool_line(
        "Bash", {"command": "pytest -q"}, status="error", tool_call_id="c1", duration_ms=5,
        exit_code=1, transport="mcp_http", preview_lines=["l1"],
    )
    payload = line.to_payload()
    assert payload["text"] == line.to_text()
    assert payload["preview_lines"] == ["l1"]
    assert ToolLine.from_payload(payload) == line


def test_from_payload_rejects_non_mapping_and_empty() -> None:
    assert ToolLine.from_payload(None) is None
    not_a_mapping: Any = "⌕ ler a"
    assert ToolLine.from_payload(not_a_mapping) is None
    assert ToolLine.from_payload({"status": "ok"}) is None


def test_from_payload_legacy_text_is_not_interpreted() -> None:
    line = ToolLine.from_payload({"text": "✓ ⚒ $ read_file x"})
    assert line is not None
    assert (line.tool, line.category, line.status, line.verb) == ("", "execution", STATUS_RUNNING, "")
    assert line.subject == "✓ ⚒ $ read_file x"
    assert line.to_text() == "$ ✓ ⚒ $ read_file x"


def test_with_outcome() -> None:
    line = build_tool_line("Bash", {"command": "pytest"}, tail="coletando")
    done = line.with_outcome(status="failed", duration_ms=1500)
    assert (done.status, done.duration_ms, done.tail) == (STATUS_ERROR, 1500, "coletando")
    assert done.with_outcome(status="ok", tail="3 passed\n").tail == "3 passed"
    assert line.status == STATUS_RUNNING


# --- glifos -----------------------------------------------------------------------

def test_glyphs_cover_every_category() -> None:
    assert set(CATEGORY_GLYPHS) == {category.value for category in ToolActivityCategory}
    assert len(set(CATEGORY_GLYPHS.values())) == len(CATEGORY_GLYPHS)


@pytest.mark.parametrize("glyph", [*CATEGORY_GLYPHS.values(), "✓", "✗", "◇"])
def test_glyphs_are_single_cell_and_not_emoji_presentation(glyph: str) -> None:
    assert cell_len(glyph) == 1
    # Toda tabela que a calibração do terminal pode escolher (cell_widths).
    for version in (*RICH_VERSIONS, EMOJI_NARROW_VERSION):
        assert get_character_cell_size(glyph, version) == 1, version
    assert unicodedata.east_asian_width(glyph) not in {"W", "F"}


# --- classificador ----------------------------------------------------------------

@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("Bash", {"command": "pytest -q tests"}, ToolActivityCategory.VALIDATION),
        ("Bash", {"command": "/bin/bash -lc 'ruff check .'"}, ToolActivityCategory.VALIDATION),
        ("run", {"input": "git commit -m x"}, ToolActivityCategory.VERSION_CONTROL),
        ("Read", None, ToolActivityCategory.INSPECTION),
        ("Edit", None, ToolActivityCategory.MODIFICATION),
        ("WebSearch", None, ToolActivityCategory.RESEARCH),
        ("Task", None, ToolActivityCategory.DELEGATION),
        ("git_status", None, ToolActivityCategory.INSPECTION),
        ("git_commit", None, ToolActivityCategory.VERSION_CONTROL),
        ("mcp__quimera__todo_write", None, ToolActivityCategory.MODIFICATION),
        ("browser_navigate", None, ToolActivityCategory.RESEARCH),
        ("host_memory", None, ToolActivityCategory.INSPECTION),
    ],
)
def test_classifier_native_cli_names(tool: str, args: dict | None, expected: ToolActivityCategory) -> None:
    assert classify_tool_activity(tool, args) is expected


def test_classifier_keeps_previous_sets() -> None:
    assert {"read_file", "list_files", "grep_search", "git_status", "git_diff", "git_log",
            "git_branch", "browser_status", "browser_network", "tui_screenshot"} <= INSPECTION_TOOLS
    assert {"apply_patch", "write_file", "remove_file"} <= MODIFICATION_TOOLS
    assert {"git_add", "git_commit", "git_checkout", "git_push", "git_fetch"} == VERSION_CONTROL_TOOLS
    assert {"web_search", "web_fetch", "browser_start", "browser_open",
            "browser_click", "browser_screenshot"} <= RESEARCH_TOOLS
    assert {"tasks_delegate", "tasks_create", "workflows_start", "workflows_replan"} <= DELEGATION_TOOLS
    assert {"exec_command", "run_shell", "exec_session", "run_command", "shell"} <= COMMAND_TOOLS
    assert "workflows_create" not in DELEGATION_TOOLS


def test_tool_sets_are_disjoint() -> None:
    sets = [INSPECTION_TOOLS, MODIFICATION_TOOLS, VERSION_CONTROL_TOOLS, RESEARCH_TOOLS,
            DELEGATION_TOOLS, COMMAND_TOOLS]
    for index, first in enumerate(sets):
        for second in sets[index + 1:]:
            assert not first & second


@pytest.mark.parametrize(
    ("tool_call_id", "namespace"),
    [
        ("toolu_01AbC", "toolu"),
        ("mcp:run1:5", "mcp"),
        ("call_abc_def", "call"),
        ("fc_123", "fc"),
        ("req:9", "req"),
        ("exec-3", ""),
        ("123_x", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_tool_call_namespace(tool_call_id, namespace):
    assert tool_call_namespace(tool_call_id) == namespace


@pytest.mark.parametrize(("value", "expected"), [(0, 0), (12, 12), (True, None), (False, None), (-1, None), ("5", None), (1.5, None)])
def test_coerce_duration_ms_rejects_bool_and_non_int(value, expected):
    assert coerce_duration_ms(value) == expected


def test_bool_duration_never_reaches_the_text():
    line = ToolLine.from_payload({**build_tool_line("read_file", {"path": "a.py"}, status="ok").to_payload(), "duration_ms": True})

    assert line is not None and line.duration_ms is None
    assert line.to_text() == "✓ ⌕ ler a.py"
