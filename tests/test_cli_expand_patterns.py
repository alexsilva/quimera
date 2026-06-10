from quimera.cli import _expand_patterns


def _base_available():
    # The available agents list used for tests; mirrors typical lowercase names
    return [
        "claude",
        "gemini",
        "opencode-nano",
        "opencode-omni",
        "opencode-gpt",
        "ollama-qwen",
        "minimax",
        "nemotron",
    ]


def test_expand_patterns_wildcard_expands_to_matching_agents():
    """Verifica que padrão com wildcard expande para agentes correspondentes."""
    available = _base_available()
    patterns = ["opencode-*", "claude"]
    result = _expand_patterns(patterns, available)
    assert result == ["opencode-nano", "opencode-omni", "opencode-gpt", "claude"]


def test_expand_patterns_preserves_order_of_first_appearance():
    """Verifica que a ordem dos padrões é preservada na expansão."""
    available = _base_available()
    patterns = ["opencode-omni", "opencode-nano", "opencode-omni"]
    result = _expand_patterns(patterns, available)
    assert result == ["opencode-omni", "opencode-nano"]


def test_expand_patterns_removes_duplicates():
    """Verifica que duplicatas são removidas do resultado."""
    available = _base_available()
    patterns = ["opencode-nano", "opencode-nano", "claude", "claude"]
    result = _expand_patterns(patterns, available)
    assert result == ["opencode-nano", "claude"]


def test_expand_patterns_wildcard_with_duplicates():
    """Verifica que wildcard com padrões duplicados remove duplicatas."""
    available = _base_available()
    patterns = ["opencode-*", "opencode-*"]
    result = _expand_patterns(patterns, available)
    assert result == ["opencode-nano", "opencode-omni", "opencode-gpt"]


def test_expand_patterns_no_wildcard_returns_exact_match():
    """Verifica que padrão sem wildcard retorna correspondência exata case-insensitive."""
    available = _base_available()
    patterns = ["opencode-nano", "CLAUDE"]  # CLAUDE should match claude after lowercasing
    result = _expand_patterns(patterns, available)
    assert result == ["opencode-nano", "claude"]


def test_expand_patterns_case_insensitive_wildcard():
    """Verifica que correspondência case-insensitive funciona sem wildcard."""
    available = _base_available()
    patterns = ["OpEnCoDe-NaNo"]  # no wildcard, ensure case-insensitive match
    result = _expand_patterns(patterns, available)
    assert result == ["opencode-nano"]
