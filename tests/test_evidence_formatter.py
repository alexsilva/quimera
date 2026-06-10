"""Tests para EvidenceFormatter."""

from quimera.evidence.models import Evidence
from quimera.evidence.formatter import EvidenceFormatter


def test_empty_list():
    """Verifica que format retorna string vazia para lista vazia."""
    result = EvidenceFormatter.format([])
    assert result == ""


def test_only_file_read():
    """Verifica que format exibe arquivos lidos na seção de contexto."""
    evidences = [
        Evidence(ts="2026-05-18T20:36:10.000Z", path="/tmp/a.txt", digest="aaa", type="file_read"),
        Evidence(ts="2026-05-18T20:36:11.000Z", path="/tmp/b.txt", digest="bbb", type="file_read"),
    ]
    result = EvidenceFormatter.format(evidences)
    assert '<evidence_context title="Contexto Compartilhado de Evidências">' in result
    assert "Estas evidências resumem arquivos já inspecionados" in result
    assert "/tmp/a.txt" in result
    assert "/tmp/b.txt" in result


def test_only_think_summary():
    """Verifica que format exibe pensamentos na seção de contexto."""
    evidences = [
        Evidence(ts="2026-05-18T20:36:10.000Z", path="", digest="", type="think_summary", summary="Analisando o código"),
        Evidence(ts="2026-05-18T20:36:11.000Z", path="", digest="", type="think_summary", summary="Preciso verificar testes"),
    ]
    result = EvidenceFormatter.format(evidences)
    assert '<evidence_context title="Contexto Compartilhado de Evidências">' in result
    assert "Analisando o código" in result
    assert "Preciso verificar testes" in result


def test_tool_call_section():
    """Verifica que format exibe tool calls na seção de execução recente."""
    evidences = [
        Evidence(
            ts="2026-05-18T20:36:10.000Z",
            path="",
            digest="",
            type="tool_call",
            summary="exec_command: ok | cmd: ls",
        ),
    ]
    result = EvidenceFormatter.format(evidences)
    assert "Execução recente" in result
    assert "exec_command: ok | cmd: ls" in result


def test_mixed_types():
    """Verifica que format suporta evidências de tipos mistos simultaneamente."""
    evidences = [
        Evidence(ts="2026-05-18T20:36:10.000Z", path="/tmp/a.txt", digest="aaa", type="file_read"),
        Evidence(ts="2026-05-18T20:36:10.500Z", path="", digest="", type="tool_call", summary="exec_command: ok | cmd: rg"),
        Evidence(ts="2026-05-18T20:36:11.000Z", path="", digest="", type="think_summary", summary="Pensamento 1"),
        Evidence(ts="2026-05-18T20:36:12.000Z", path="/tmp/b.txt", digest="bbb", type="file_edit"),
    ]
    result = EvidenceFormatter.format(evidences)
    assert '<evidence_context title="Contexto Compartilhado de Evidências">' in result
    assert "Arquivos visitados" in result
    assert "Execução recente" in result
    assert "Pensamentos" in result
    assert "/tmp/a.txt" in result
    assert "/tmp/b.txt" in result


def test_unique_paths_most_recent():
    """Verifica que paths duplicados são deduplicados, mantendo apenas o mais recente."""
    evidences = [
        Evidence(ts="2026-05-18T20:36:10.000Z", path="/tmp/a.txt", digest="aaa", type="file_read"),
        Evidence(ts="2026-05-18T20:36:11.000Z", path="/tmp/b.txt", digest="bbb", type="file_read"),
        Evidence(ts="2026-05-18T20:36:12.000Z", path="/tmp/a.txt", digest="ccc", type="file_edit"),
    ]
    result = EvidenceFormatter.format(evidences)
    lines = result.split("\n")
    paths = [line.replace("- ", "") for line in lines if line.startswith("- /")]
    assert paths.count("/tmp/a.txt") == 1


def test_truncation():
    """Verifica que o resultado é truncado quando excede max_chars."""
    long_summary = "A" * 500
    evidences = [
        Evidence(ts="2026-05-18T20:36:10.000Z", path="/tmp/a.txt", digest="aaa", type="file_read"),
        Evidence(ts="2026-05-18T20:36:11.000Z", path="", digest="", type="think_summary", summary=long_summary),
    ]
    result = EvidenceFormatter.format(evidences, max_chars=500)
    assert len(result) <= 500


def test_think_summary_truncation_200_chars():
    """Verifica que summaries longos são limitados a 200 caracteres."""
    long_text = "X" * 300
    evidences = [
        Evidence(ts="2026-05-18T20:36:10.000Z", path="", digest="", type="think_summary", summary=long_text),
    ]
    result = EvidenceFormatter.format(evidences)
    think_line = next(line for line in result.split("\n") if line.startswith("- "))
    assert len(think_line) <= 204
