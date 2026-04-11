import pytest
from unittest.mock import patch
from quimera.runtime.parser import extract_tool_call, strip_tool_block, _parse_json_object, ToolCallParseError

def test_extract_tool_call_none():
    assert extract_tool_call(None) is None
    assert extract_tool_call("no tool here") is None

def test_extract_tool_call_tag_format():
    response = '<tool function="run_shell" command="pwd" />'
    call = extract_tool_call(response)
    assert call.name == "run_shell"
    assert call.arguments == {"command": "pwd"}

def test_extract_tool_call_tag_with_json_arguments():
    response = "<tool function=\"read_file\" arguments='{\"path\": \"foo.txt\"}' />"
    call = extract_tool_call(response)
    assert call.name == "read_file"
    assert call.arguments == {"path": "foo.txt"}

def test_extract_tool_call_tag_block_with_body_json():
    response = '<tool function="list_files">\n{"path": "."}\n</tool>'
    call = extract_tool_call(response)
    assert call.name == "list_files"
    assert call.arguments == {"path": "."}

def test_extract_tool_call_invalid_json():
    response = '<tool function="read_file" arguments="{invalid json}" />'
    with pytest.raises(ToolCallParseError, match="Payload de tool inválido"):
        extract_tool_call(response)

def test_extract_tool_call_no_json_object():
    # Line 21 coverage: No { found
    with pytest.raises(ToolCallParseError, match="Nenhum objeto JSON encontrado no payload da tool"):
        _parse_json_object("no curly braces")

def test_extract_tool_call_not_a_dict():
    # Line 28 coverage: obj is not a dict
    # We mock raw_decode because any JSON starting with '{' will be a dict
    with patch("json.JSONDecoder.raw_decode") as mock_decode:
        mock_decode.return_value = (["not", "a", "dict"], 0)
        response = '<tool function="list_files">{ "this": "will be mocked" }</tool>'
        with pytest.raises(ToolCallParseError, match="Payload de tool inválido: esperado objeto JSON"):
            extract_tool_call(response)

def test_strip_tool_tag_block():
    response = 'Text before <tool function="run_shell" command="pwd" /> Text after'
    assert strip_tool_block(response) == "Text before  Text after"
