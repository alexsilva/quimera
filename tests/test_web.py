"""Testes unitários para WebTool (web_search e web_fetch)."""

from unittest.mock import patch, MagicMock

import json
import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.web import WebTool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def web_tool(tmp_path):
    """Retorna uma instância de WebTool com workspace temporário."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    return WebTool(config)


@pytest.fixture
def mock_curl():
    """Mocka WebTool._curl para evitar dependência externa."""
    with patch.object(WebTool, "_curl") as mock:
        yield mock


# ---------------------------------------------------------------------------
# web_search – sucesso com DuckDuckGo Lite HTML
# ---------------------------------------------------------------------------

DUCK_HTML_SAMPLE = """\
<html>
<body>
<form action="/lite/" method="post">
  <input name="q" type="text" />
  <input type="submit" value="Search" />
</form>
<table>
  <tr>
    <td class="result-snippet">Exemplo de snippet 1</td>
    <td><a class="result-link" href="https://exemplo1.com">Título 1</a></td>
  </tr>
  <tr>
    <td class="result-snippet">Snippet dois</td>
    <td><a class="result-link" href="https://exemplo2.org">Título 2</a></td>
  </tr>
</table>
</body>
</html>
"""


def test_web_search_returns_links(web_tool, mock_curl):
    """web_search retorna lista de links do HTML do DuckDuckGo Lite."""
    mock_curl.return_value = DUCK_HTML_SAMPLE

    result = web_tool.web_search(ToolCall(
        name="web_search",
        arguments={"query": "teste", "count": 2},
    ))

    assert result.ok is True
    data = result.data
    assert data["total"] == 2
    assert len(data["results"]) == 2
    assert data["results"][0]["title"] == "Título 1"
    assert data["results"][0]["url"] == "https://exemplo1.com"
    assert data["results"][0]["snippet"] == "Exemplo de snippet 1"


def test_web_search_empty_query(web_tool, mock_curl):
    """Query vazia retorna erro sem chamar curl."""
    result = web_tool.web_search(ToolCall(
        name="web_search",
        arguments={"query": ""},
    ))

    assert result.ok is False
    assert "obrigatório" in result.error.lower() or "query" in result.error.lower()
    mock_curl.assert_not_called()


def test_web_search_uses_configured_urls(web_tool, mock_curl):
    """Confirma que as URLs das constantes da classe são usadas."""
    mock_curl.return_value = "<html></html>"

    web_tool.web_search(ToolCall(
        name="web_search",
        arguments={"query": "python"},
    ))

    # Deve chamar curl com a URL do DuckDuckGo Lite
    called_url = mock_curl.call_args_list[0][0][0]
    assert "lite.duckduckgo.com/lite/" in called_url
    assert "q=python" in called_url


def test_web_search_fallback_json(web_tool, mock_curl):
    """Quando o HTML não retorna links, tenta fallback JSON da API."""
    mock_curl.side_effect = [
        "<html></html>",  # Primeira chamada (lite) – vazio
        json.dumps({      # Segunda chamada (API JSON)
            "AbstractText": "Linguagem de programação Python",
            "AbstractSource": "Wikipedia",
            "AbstractURL": "https://pt.wikipedia.org/wiki/Python",
        }),
    ]

    result = web_tool.web_search(ToolCall(
        name="web_search",
        arguments={"query": "python"},
    ))

    assert result.ok is True
    assert result.data["total"] >= 1
    assert "Python" in result.data["results"][0]["snippet"]


def test_web_search_curl_error(web_tool, mock_curl):
    """Falha no curl retorna ToolResult com ok=False."""
    mock_curl.side_effect = TimeoutError("curl timeout")

    result = web_tool.web_search(ToolCall(
        name="web_search",
        arguments={"query": "teste"},
    ))

    assert result.ok is False
    assert "timeout" in result.error.lower()


# ---------------------------------------------------------------------------
# web_fetch – download de URLs
# ---------------------------------------------------------------------------

HTML_SAMPLE = """\
<html><body>
<h1>Título</h1>
<p>Parágrafo de <b>exemplo</b>.</p>
</body></html>
"""


def test_web_fetch_strips_html(web_tool, mock_curl):
    """web_fetch retorna texto limpo (sem tags HTML)."""
    mock_curl.return_value = HTML_SAMPLE

    result = web_tool.web_fetch(ToolCall(
        name="web_fetch",
        arguments={"urls": "https://exemplo.com"},
    ))

    assert result.ok is True
    assert result.data["total"] == 1
    content = result.data["results"][0]["content"]
    assert "<h1>" not in content
    assert "<b>" not in content
    assert "Título" in content
    assert "exemplo" in content


def test_web_fetch_raw_mode(web_tool, mock_curl):
    """Com raw=True, retorna HTML puro."""
    mock_curl.return_value = HTML_SAMPLE

    result = web_tool.web_fetch(ToolCall(
        name="web_fetch",
        arguments={"urls": "https://exemplo.com", "raw": True},
    ))

    assert result.ok is True
    content = result.data["results"][0]["content"]
    assert "<h1>Título</h1>" in content


def test_web_fetch_empty_urls(web_tool, mock_curl):
    """Lista vazia de URLs retorna erro."""
    result = web_tool.web_fetch(ToolCall(
        name="web_fetch",
        arguments={"urls": ""},
    ))

    assert result.ok is False
    assert "nenhuma" in result.error.lower() or "fornecida" in result.error.lower()
    mock_curl.assert_not_called()


def test_web_fetch_resolves_url(web_tool, mock_curl):
    """URL sem esquema recebe https:// prefixo."""
    mock_curl.return_value = "conteudo"

    result = web_tool.web_fetch(ToolCall(
        name="web_fetch",
        arguments={"urls": "exemplo.com"},
    ))

    assert result.ok is True
    called_url = mock_curl.call_args[0][0]
    assert called_url.startswith("https://")


def test_web_fetch_multiple_urls(web_tool, mock_curl):
    """Múltiplas URLs são baixadas e retornadas."""
    mock_curl.side_effect = ["pagina a", "pagina b"]

    result = web_tool.web_fetch(ToolCall(
        name="web_fetch",
        arguments={"urls": ["https://a.com", "https://b.com"]},
    ))

    assert result.ok is True
    assert result.data["total"] == 2
    assert mock_curl.call_count == 2


def test_web_fetch_respects_max_urls(web_tool, mock_curl):
    """Mais de _MAX_URLS é truncado."""
    mock_curl.side_effect = [f"pagina {i}" for i in range(10)]

    result = web_tool.web_fetch(ToolCall(
        name="web_fetch",
        arguments={"urls": [f"https://site{i}.com" for i in range(10)]},
    ))

    assert result.ok is True
    assert result.data["total"] == web_tool._MAX_URLS
    assert mock_curl.call_count == web_tool._MAX_URLS


# ---------------------------------------------------------------------------
# _resolve_url
# ---------------------------------------------------------------------------

def test_resolve_url_adds_https(web_tool):
    """URL sem esquema ganha https:// prefixo."""
    assert web_tool._resolve_url("exemplo.com") == "https://exemplo.com"


def test_resolve_url_preserves_scheme(web_tool):
    """URL com http:// é mantida."""
    assert web_tool._resolve_url("http://exemplo.com") == "http://exemplo.com"


def test_resolve_url_preserves_https(web_tool):
    """URL com https:// é mantida."""
    assert web_tool._resolve_url("https://exemplo.com") == "https://exemplo.com"


def test_resolve_url_rejects_empty(web_tool):
    """URL vazia levanta ValueError."""
    with pytest.raises(ValueError, match="vazia"):
        web_tool._resolve_url("")
    with pytest.raises(ValueError):
        web_tool._resolve_url("   ")


# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------

def test_strip_html_removes_scripts(web_tool):
    """Tags <script> são removidas."""
    html = "<script>alert('xss')</script><p>texto</p>"
    assert "alert" not in web_tool._strip_html(html)
    assert "texto" in web_tool._strip_html(html)


def test_strip_html_removes_styles(web_tool):
    """Tags <style> são removidas."""
    html = "<style>body{color:red}</style><p>texto</p>"
    assert "color" not in web_tool._strip_html(html)
    assert "texto" in web_tool._strip_html(html)


def test_strip_html_preserves_text(web_tool):
    """Texto entre tags é preservado."""
    html = "<div><p>Hello <b>World</b></p></div>"
    result = web_tool._strip_html(html)
    assert "Hello World" in result
