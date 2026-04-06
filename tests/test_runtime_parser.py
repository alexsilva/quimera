import pytest
import json
from unittest.mock import patch
from quimera.runtime.parser import extract_tool_call, strip_tool_block, _parse_json_object, ToolCallParseError

def test_extract_tool_call_valid():
    response = 'Here is the tool call: ```tool {"name": "test", "arguments": {"foo": "bar"}} ```'
    call = extract_tool_call(response)
    assert call.name == "test"
    assert call.arguments == {"foo": "bar"}

def test_extract_tool_call_none():
    assert extract_tool_call(None) is None
    assert extract_tool_call("no tool here") is None

def test_extract_tool_call_invalid_json():
    response = '```tool {invalid json} ```'
    with pytest.raises(ToolCallParseError, match="Bloco tool inválido"):
        extract_tool_call(response)

def test_extract_tool_call_no_json_object():
    # Line 21 coverage: No { found
    with pytest.raises(ToolCallParseError, match="Nenhum objeto JSON encontrado no bloco tool"):
        _parse_json_object("no curly braces")

def test_extract_tool_call_not_a_dict():
    # Line 28 coverage: obj is not a dict
    # We mock raw_decode because any JSON starting with '{' will be a dict
    with patch("json.JSONDecoder.raw_decode") as mock_decode:
        mock_decode.return_value = (["not", "a", "dict"], 0)
        response = '```tool { "this": "will be mocked" } ```'
        with pytest.raises(ToolCallParseError, match="Bloco tool inválido: esperado objeto JSON"):
            extract_tool_call(response)

def test_extract_tool_call_invalid_payload():
    response = '```tool {"name": 123, "arguments": "not a dict"} ```'
    with pytest.raises(ToolCallParseError, match="Payload de tool inválido"):
        extract_tool_call(response)

def test_strip_tool_block():
    response = 'Text before ```tool {"name": "test"} ``` Text after'
    assert strip_tool_block(response) == "Text before  Text after"
