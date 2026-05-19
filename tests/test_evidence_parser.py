"""Testes para o parser de evidências."""

from quimera.evidence.parser import (
    FileEditExtractor,
    FileReadExtractor,
    PatternRegistry,
    ThinkExtractor,
)

SAMPLE_AGENT = "test-agent"
SAMPLE_SESSION = "sess-001"


class TestThinkExtractor:
    def test_extract_single_think_block(self):
        output = "Some text\n<thinking>Este é o raciocínio do modelo sobre o problema.</thinking>\nMore text."
        ext = ThinkExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].type == "think_summary"
        assert "raciocínio do modelo" in results[0].summary
        assert results[0].agent == SAMPLE_AGENT
        assert results[0].session_id == SAMPLE_SESSION

    def test_extract_think_tag(self):
        output = "<think>Conteúdo com think tag.</think>"
        ext = ThinkExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].type == "think_summary"
        assert "Conteúdo com think tag" in results[0].summary

    def test_extract_multiple_think_blocks(self):
        output = "<thinking>Primeiro bloco.</thinking>\ntexto\n<think>Segundo bloco.</think>"
        ext = ThinkExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 2
        assert "Primeiro bloco" in results[0].summary
        assert "Segundo bloco" in results[1].summary

    def test_truncates_to_500_chars(self):
        long_text = "A" * 1200
        output = f"<thinking>{long_text}</thinking>"
        ext = ThinkExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert len(results[0].summary) == 500

    def test_no_think_block_returns_empty(self):
        output = "Apenas texto normal sem blocos de raciocínio."
        ext = ThinkExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 0


class TestFileReadExtractor:
    def test_read_file_pattern(self):
        output = "Read file: /home/project/src/main.py"
        ext = FileReadExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].type == "file_read"
        assert results[0].path == "/home/project/src/main.py"

    def test_lendo_pattern(self):
        output = "Lendo /tmp/config.yaml para análise."
        ext = FileReadExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].type == "file_read"
        assert results[0].path == "/tmp/config.yaml"

    def test_read_pattern(self):
        output = "Read src/utils.py"
        ext = FileReadExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].type == "file_read"
        assert results[0].path == "src/utils.py"

    def test_deduplicates_paths(self):
        output = "Read file: /a.txt\nRead file: /a.txt\nLendo /b.txt"
        ext = FileReadExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 2
        paths = {e.path for e in results}
        assert paths == {"/a.txt", "/b.txt"}

    def test_no_match_returns_empty(self):
        output = "Nenhum arquivo foi lido nesta execução."
        ext = FileReadExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 0


class TestFileEditExtractor:
    def test_checkmark_edit_pattern(self):
        output = "✓ Edit src/models.py"
        ext = FileEditExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].type == "file_edit"
        assert results[0].path == "src/models.py"

    def test_edit_pattern(self):
        output = "Edit README.md"
        ext = FileEditExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].type == "file_edit"
        assert results[0].path == "README.md"

    def test_wrote_pattern(self):
        output = "Wrote tests/test_parser.py"
        ext = FileEditExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].type == "file_edit"
        assert results[0].path == "tests/test_parser.py"

    def test_deduplicates_paths(self):
        output = "✓ Edit /a.py\nEdit /a.py\nWrote /b.py"
        ext = FileEditExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 2
        paths = {e.path for e in results}
        assert paths == {"/a.py", "/b.py"}

    def test_no_match_returns_empty(self):
        output = "Nenhuma edição foi realizada."
        ext = FileEditExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 0


class TestPatternRegistry:
    def test_register_and_extract_all(self):
        PatternRegistry._extractors = {}
        PatternRegistry.default()

        output = (
            "<thinking>Raciocínio inicial.</thinking>\n"
            "Read file: /src/a.py\n"
            "✓ Edit /src/a.py\n"
        )
        results = PatternRegistry.extract_all(output, SAMPLE_AGENT, SAMPLE_SESSION)

        types = {e.type for e in results}
        assert "think_summary" in types
        assert "file_read" in types
        assert "file_edit" in types

    def test_extract_all_empty_registry(self):
        PatternRegistry._extractors = {}
        results = PatternRegistry.extract_all("qualquer texto", SAMPLE_AGENT, SAMPLE_SESSION)
        assert len(results) == 0

    def test_extract_all_propagates_agent_and_session(self):
        PatternRegistry._extractors = {}
        PatternRegistry.default()

        output = "Read file: /x.txt"
        results = PatternRegistry.extract_all(output, "my-agent", "my-session")

        for ev in results:
            assert ev.agent == "my-agent"
            assert ev.session_id == "my-session"
