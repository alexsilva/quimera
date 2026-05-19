"""Testes para o parser de evidências."""

from quimera.evidence.parser import (
    FileEditExtractor,
    FileReadExtractor,
    PatternRegistry,
    ThinkExtractor,
    _sanitize_path,
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


class TestSanitizePath:
    """Testes para a função de sanitização de paths."""

    def test_valid_path_passes_through(self):
        assert _sanitize_path("src/main.py") == "src/main.py"
        assert _sanitize_path("/home/user/file.txt") == "/home/user/file.txt"
        assert _sanitize_path("./config.yaml") == "./config.yaml"

    def test_strips_trailing_checkmark(self):
        assert _sanitize_path("quimera/prompt.py✓") == "quimera/prompt.py"
        assert _sanitize_path("src/models.py✓") == "src/models.py"

    def test_strips_leading_checkmark(self):
        assert _sanitize_path("✓src/models.py") == "src/models.py"

    def test_strips_ansi_codes(self):
        ansi_path = "\x1b[32mquimera/prompt.py\x1b[0m"
        assert _sanitize_path(ansi_path) == "quimera/prompt.py"

    def test_rejects_plain_word_no_path_structure(self):
        assert _sanitize_path("Read") is None
        assert _sanitize_path("Edit") is None
        assert _sanitize_path("hello") is None

    def test_rejects_empty_and_whitespace(self):
        assert _sanitize_path("") is None
        assert _sanitize_path("   ") is None

    def test_strips_surrounding_noise_chars(self):
        assert _sanitize_path("[src/main.py]") == "src/main.py"
        assert _sanitize_path("`config.yaml`") == "config.yaml"

    def test_rejects_path_with_invalid_chars(self):
        assert _sanitize_path("file name.py") is None
        assert _sanitize_path("file\nname.py") is None


class TestNoisyStdoutScenarios:
    """Testes cobrindo cenários reais de stdout com ruído de renderização."""

    def test_checkmark_attached_to_read_path(self):
        """Caso: 'Read file: quimera/prompt.py✓' - checkmark colado no path."""
        output = "Read file: quimera/prompt.py✓"
        ext = FileReadExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].path == "quimera/prompt.py"

    def test_edit_path_concatenated_with_next_event_word(self):
        """Caso: 'Edit quimera/evidence/formatter.pyRead' - concatenação de linhas.

        'Read' vem do próximo evento e não tem estrutura de path,
        então o path completo é rejeitado pela validação.
        """
        output = "Edit quimera/evidence/formatter.pyRead"
        ext = FileEditExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 0

    def test_ansi_colored_output(self):
        """Output com cores ANSI em volta do path."""
        output = "\x1b[32mRead file: src/main.py\x1b[0m"
        ext = FileReadExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].path == "src/main.py"

    def test_realistic_noisy_stdout(self):
        """Stdout realista com múltiplos artefatos misturados."""
        output = (
            "\x1b[1mAgent started\x1b[0m\n"
            "Read file: quimera/prompt.py✓\n"
            "✓ Edit quimera/evidence/parser.py\n"
            "Read quimera/evidence/formatter.py\n"
            "Wrote tests/test_parser.py\n"
            "\x1b[32mDone\x1b[0m"
        )
        ext_read = FileReadExtractor()
        ext_edit = FileEditExtractor()

        read_results = ext_read.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)
        edit_results = ext_edit.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        read_paths = {e.path for e in read_results}
        assert read_paths == {"quimera/prompt.py", "quimera/evidence/formatter.py"}

        edit_paths = {e.path for e in edit_results}
        assert edit_paths == {"quimera/evidence/parser.py", "tests/test_parser.py"}

    def test_line_merge_with_checkmark_between_events(self):
        """Duas linhas fundidas: path + checkmark + próximo evento."""
        output = "Read file: src/a.py✓ Edit src/b.py"
        ext = FileReadExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].path == "src/a.py"

    def test_bracket_artifacts_around_path(self):
        """Path envolto em brackets de template markdown."""
        output = "Read file: [src/config.yaml]"
        ext = FileReadExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].path == "src/config.yaml"

    def test_multiple_noise_chars_stripped(self):
        """Vários caracteres de ruído ao redor do path."""
        output = "Edit ``src/utils.py``;"
        ext = FileEditExtractor()
        results = ext.extract(output, SAMPLE_AGENT, SAMPLE_SESSION)

        assert len(results) == 1
        assert results[0].path == "src/utils.py"
