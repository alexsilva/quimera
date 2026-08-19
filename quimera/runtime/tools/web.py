"""Componentes de `quimera.runtime.tools.web`."""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import tempfile
from html import unescape
import urllib.parse
from pathlib import Path

from quimera import process_factory as subprocess

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError
from .base import ToolBase, ValidatableTool

_logger = logging.getLogger(__name__)


def fetch_url_text(url: str, *, timeout: int = 30) -> str:
    """Baixa uma URL publica e retorna o texto sem HTML.

    Aplica as mesmas guardas do web_fetch (apenas http/https, IPs privados
    bloqueados contra SSRF). Levanta ValueError para URL invalida ou bloqueada
    e TimeoutError/OSError em falhas de rede.
    """
    cleaned = str(url or "").strip()
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"URL invalida: {cleaned}")
    try:
        ip = socket.gethostbyname(parsed.hostname)
        if ipaddress.ip_address(ip).is_private:
            raise ValueError(f"Acesso a IP privado nao permitido (SSRF): {ip}")
    except OSError as exc:
        raise OSError(f"Falha ao resolver host: {parsed.hostname}") from exc
    args = [
        "curl",
        "-s",
        "--max-redirs",
        "0",
        "-m",
        str(timeout),
        "-A",
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
        cleaned,
    ]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"curl excedeu o tempo limite de {timeout}s.")
    return WebTool._strip_html(result.stdout)


class WebTool(ToolBase, tool_prefix="web", tool_public_methods=("http_request",)):
    """Implementa `WebTool` — busca na web usando curl."""
    _MAX_URLS = 5
    _HTTP_SCHEME = "http://"
    _HTTPS_SCHEME = "https://"
    _HTTPS_SCHEME_PREFIX = "https:"
    _URL_SCHEMES = (_HTTP_SCHEME, _HTTPS_SCHEME)
    _DUCKDUCKGO_DOMAIN = "duckduckgo.com"
    _DUCKDUCKGO_BASE_URL = "https://duckduckgo.com"
    _DUCKDUCKGO_LITE_URL = "https://lite.duckduckgo.com/lite/"
    _DUCKDUCKGO_API_URL = "https://api.duckduckgo.com/"

    def __init__(self, config: ToolRuntimeConfig) -> None:
        super().__init__(config)

    def _resolve_url(self, raw: str) -> str:
        raw = raw.strip()
        if not raw:
            raise ValueError("URL vazia")
        if not raw.startswith(self._URL_SCHEMES):
            raw = self._HTTPS_SCHEME + raw
        return raw


    def http_request(self, call: ToolCall) -> ToolResult:
        """Requisição HTTP genérica (GET/POST/PUT/PATCH/DELETE) com corpo e headers."""
        try:
            url, parsed, resolved_ip = self._resolve_public_http_target(
                str(call.arguments.get("url", ""))
            )
        except ValueError as exc:
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))
        method_name = str(call.arguments.get("method", "GET")).strip().upper() or "GET"
        if method_name not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            return ToolResult(ok=False, tool_name=call.name, error=f"método HTTP não suportado: {method_name}")
        timeout = int(call.arguments.get("timeout", 30) or 30)
        timeout = max(1, min(timeout, 120))
        headers = call.arguments.get("headers") or {}
        if not isinstance(headers, dict):
            return ToolResult(ok=False, tool_name=call.name, error="headers deve ser um objeto")
        body = call.arguments.get("body")
        json_body = call.arguments.get("json")
        args = [
            "curl", "-sS", "-i", "--max-redirs", "0", "-m", str(timeout),
            "-A", "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "-X", method_name,
        ]
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        curl_ip = f"[{resolved_ip}]" if ":" in resolved_ip else resolved_ip
        args.extend(["--resolve", f"{host}:{port}:{curl_ip}"])
        for key, value in headers.items():
            args.extend(["-H", f"{key}: {value}"])
        if json_body is not None:
            payload = json.dumps(json_body, ensure_ascii=False)
            args.extend(["-H", "Content-Type: application/json", "-d", payload])
        elif body is not None:
            args.extend(["-d", str(body)])
        args.append(url)
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, tool_name=call.name, error=f"http_request excedeu timeout de {timeout}s")
        raw = result.stdout or ""
        header_text, _, body_text = raw.partition("\r\n\r\n")
        if not body_text:
            header_text, _, body_text = raw.partition("\n\n")
        status_code = None
        first = header_text.splitlines()[0] if header_text else ""
        parts = first.split()
        if len(parts) >= 2 and parts[1].isdigit():
            status_code = int(parts[1])
        max_chars = 50_000
        truncated = len(body_text) > max_chars
        visible = body_text[:max_chars]
        payload = {
            "url": url,
            "method": method_name,
            "status_code": status_code,
            "headers": header_text,
            "body": visible,
            "truncated": truncated,
            "curl_exit_code": result.returncode,
        }
        ok = result.returncode == 0 and status_code is not None and 200 <= status_code < 400
        return ToolResult(
            ok=ok,
            tool_name=call.name,
            content=visible if visible else (result.stderr or header_text),
            exit_code=status_code,
            truncated=truncated,
            error=None if ok else (result.stderr or first or "http_request falhou"),
            data=payload,
        )

    def _resolve_public_http_target(
        self, raw_url: str
    ) -> tuple[str, urllib.parse.ParseResult, str]:
        """Valida URL pública e fixa um endereço já verificado para evitar DNS rebinding."""
        url = self._resolve_url(raw_url)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("http_request requer URL http/https válida com host")
        try:
            infos = socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ValueError(f"Falha ao resolver host: {parsed.hostname}") from exc
        addresses: list[str] = []
        for info in infos:
            address = str(info[4][0])
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise ValueError(f"Falha ao resolver host: {parsed.hostname}")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise ValueError(
                    f"Acesso a IP não público não permitido (SSRF): {address}"
                )
        return url, parsed, addresses[0]

    def web_search(self, call: ToolCall) -> ToolResult:
        """Executa uma busca na web usando curl.

        Argumentos:
            query (str): Termo de busca.
            num_results/count (int, opcional): Número de resultados (padrão 5, máx 10).

        Usa duckduckgo HTML (lite) para obter resultados.
        """
        query = str(call.arguments.get("query", ""))
        raw_count = call.arguments.get("num_results", call.arguments.get("count", 5))
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 5
        count = max(1, min(count, 10))

        if not query.strip():
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="Parâmetro 'query' é obrigatório.",
            )

        payload = urllib.parse.urlencode({"q": query})

        try:
            result = self._write_and_curl(
                self._DUCKDUCKGO_LITE_URL,
                payload,
                content_type="application/x-www-form-urlencoded",
                timeout=15,
            )

            links = self._parse_duckduckgo_links(result, count)

            if not links:
                url_json = f"{self._DUCKDUCKGO_API_URL}?{urllib.parse.urlencode({'q': query, 'format': 'json'})}"
                result_json = self._curl(url_json, timeout=10)
                if result_json and result_json.strip():
                    data = json.loads(result_json)
                    abstract = data.get("AbstractText", "")
                    source = data.get("AbstractSource", "")
                    link = data.get("AbstractURL", "")

                    if abstract:
                        links.append({
                            "title": source or abstract[:50],
                            "url": link,
                            "snippet": abstract,
                        })

            lines = []
            for r in links:
                lines.append(f"[{r['title']}]({r['url']})")
                if r.get("snippet"):
                    lines.append(r["snippet"])
                lines.append("")
            content = "\n".join(lines).strip()

            return ToolResult(
                ok=True,
                tool_name=call.name,
                content=content,
                data={"results": links, "total": len(links)},
            )

        except Exception as exc:
            _logger.exception("Falha na busca web.")
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=f"Falha na busca web: {exc}",
            )

    def web_fetch(self, call: ToolCall) -> ToolResult:
        """Faz download do conteúdo de uma URL.

        Argumentos:
            url (str): URL para baixar.
            raw (bool, opcional): Se True, retorna HTML puro. Padrão False (extrai texto).
            timeout (int, opcional): Timeout em segundos. Padrão 30.
        """
        raw_url = call.arguments.get("url", "")
        raw_mode = bool(call.arguments.get("raw", False))
        timeout = int(call.arguments.get("timeout", 30))

        if not isinstance(raw_url, str) or not raw_url.strip():
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="Nenhuma URL fornecida.",
            )

        url = self._resolve_url(raw_url.strip())
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname:
            try:
                ip = socket.gethostbyname(parsed.hostname)
                if ipaddress.ip_address(ip).is_private:
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"Acesso a IP privado não permitido (SSRF): {ip}",
                    )
            except OSError:
                pass
        try:
            html = self._curl(url, timeout=timeout)
            if raw_mode:
                text = html
            else:
                text = self._strip_html(html)

            return ToolResult(
                ok=True,
                tool_name=call.name,
                content=text,
                data={
                    "results": [{"url": url, "content": text, "length": len(text)}],
                    "total": 1,
                },
            )
        except Exception as exc:
            _logger.exception("Falha ao baixar URL: %s", url)
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=str(exc),
                data={"results": [{"url": url, "error": str(exc)}], "total": 1},
            )

    def _curl(self, url: str, timeout: int = 30) -> str:
        """Executa curl e retorna o corpo da resposta."""
        args = [
            "curl",
            "-s",
            "--max-redirs",
            "0",
            "-m",
            str(timeout),
            "-A",
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
            url,
        ]
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
            )
            return result.stdout
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"curl excedeu o tempo limite de {timeout}s.")

    def _write_and_curl(self, url: str, payload: str, content_type: str = "application/json", timeout: int = 30) -> str:
        """Escreve payload em um arquivo temporário e faz POST via curl."""
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".payload", delete=False)
        try:
            tmp.write(payload)
            tmp.close()
            args = [
                "curl",
                "-s",
                "-L",
                "-m",
                str(timeout),
                "-X", "POST",
                "-H", f"Content-Type: {content_type}",
                "-d", f"@{tmp.name}",
                "-A",
                "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                url,
            ]
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
            )
            return result.stdout
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"curl excedeu o tempo limite de {timeout}s.")
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    @staticmethod
    def _parse_duckduckgo_links(html: str, count: int) -> list[dict]:
        """Extrai links e snippets do HTML do DuckDuckGo Lite."""
        links: list[dict] = []

        href_pattern = re.compile(
            r"""<a(?=[^>]*class=['"]result-link['"])[^>]*href=['"]([^'"]+)['"][^>]*>(.*?)</a>""",
            re.DOTALL,
        )
        snippet_pattern = re.compile(
            r"""<td[^>]*class=['"]result-snippet['"]>(.*?)</td>""",
            re.DOTALL,
        )

        hrefs = href_pattern.findall(html)
        snippets = snippet_pattern.findall(html)

        for i, (raw_url, title) in enumerate(hrefs):
            if i >= count:
                break
            snippet = ""
            if i < len(snippets):
                snippet = unescape(re.sub(r"<[^>]+>", "", snippets[i])).strip()
            title_clean = unescape(re.sub(r"<[^>]+>", "", title)).strip()
            url = WebTool._normalize_duckduckgo_url(raw_url)
            if not url:
                continue
            links.append({
                "title": title_clean or url,
                "url": url,
                "snippet": snippet,
            })

        return links

    @staticmethod
    def _normalize_duckduckgo_url(raw_url: str) -> str:
        """Normaliza URLs de resultado do DuckDuckGo, inclusive redirects internos."""
        cleaned = unescape((raw_url or "").strip())
        if not cleaned:
            return ""
        if cleaned.startswith("//"):
            cleaned = WebTool._HTTPS_SCHEME_PREFIX + cleaned
        if cleaned.startswith("/"):
            cleaned = WebTool._DUCKDUCKGO_BASE_URL + cleaned

        parsed = urllib.parse.urlparse(cleaned)
        if WebTool._DUCKDUCKGO_DOMAIN in parsed.netloc and parsed.path.startswith("/l/"):
            qs = urllib.parse.parse_qs(parsed.query)
            uddg = qs.get("uddg", [])
            if uddg:
                target = urllib.parse.unquote(uddg[0]).strip()
                if target.startswith(WebTool._URL_SCHEMES):
                    return target

        if cleaned.startswith(WebTool._URL_SCHEMES):
            return cleaned
        return ""

    @staticmethod
    def _strip_html(html: str) -> str:
        """Remove tags HTML e normaliza espaços."""
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text


class WebToolValidator(ValidatableTool):
    """Validação de policy para as ferramentas web."""

    def _validate_web_search(self, call: ToolCall) -> None:
        """Valida uma chamada de busca na web."""
        query = call.arguments.get("query", "")
        if not query or not str(query).strip():
            raise ToolPolicyError("web_search requer 'query' não vazia")

    def _validate_web_fetch(self, call: ToolCall) -> None:
        """Valida uma chamada de fetch de URL."""
        url = call.arguments.get("url")
        if isinstance(url, str) and url.strip():
            return
        raise ToolPolicyError("web_fetch requer 'url' não vazia")

    def _validate_http_request(self, call: ToolCall) -> None:
        url = str(call.arguments.get("url", "")).strip()
        if not url:
            raise ToolPolicyError("http_request requer 'url'")
        resolved = url if url.startswith(("http://", "https://")) else f"https://{url}"
        parsed = urllib.parse.urlparse(resolved)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ToolPolicyError("http_request requer URL http/https válida com host")
        method_name = str(call.arguments.get("method", "GET")).strip().upper() or "GET"
        if method_name not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            raise ToolPolicyError(f"http_request.method inválido: {method_name}")
        headers = call.arguments.get("headers")
        if headers is not None and not isinstance(headers, dict):
            raise ToolPolicyError("http_request.headers deve ser objeto")
        if "timeout" in call.arguments:
            try:
                t = int(call.arguments["timeout"])
            except (TypeError, ValueError) as exc:
                raise ToolPolicyError("http_request.timeout deve ser inteiro positivo") from exc
            if t <= 0:
                raise ToolPolicyError("http_request.timeout deve ser inteiro positivo")



_WEB_TOOL_NAMES = [
    "web_search",
    "web_fetch",
    "http_request",
]


def register(registry, policy, config) -> None:
    """Registra todas as tools web no registry e a validação na policy."""
    web_tool = WebTool(config)
    web_validator = WebToolValidator(config)
    for name in _WEB_TOOL_NAMES:
        registry.register(name, getattr(web_tool, name))
    policy.register_tool_validator(list(_WEB_TOOL_NAMES), web_validator)
