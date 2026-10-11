"""Deferral de schemas de tools: core essencial sempre exposto + descoberta sob demanda.

O catálogo completo de tools custa dezenas de milhares de caracteres por
requisição e degrada a seleção de ferramenta nos modelos de contexto curto.
Com deferral ativo, o modelo recebe apenas o conjunto essencial com schema
completo; as demais tools aparecem como um catálogo de uma linha dentro da
descrição de ``tool_search`` e têm o schema entregue (e a tool habilitada)
quando o modelo pede.

O deferral é filtro de apresentação, não fronteira de segurança: allowlist,
denylist e policy continuam sendo aplicadas sobre o catálogo completo antes
deste módulo, e uma tool diferida continua executável por nome.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Iterable, Mapping

TOOL_SEARCH_TOOL_NAME = "tool_search"
TOOL_DEFERRAL_MODES = frozenset({"off", "api", "all"})
# Chave do estado por conexão MCP com os nomes já habilitados via tool_search.
DEFERRAL_STATE_KEY = "quimera_deferral_enabled"
# Override por conexão MCP enviado no prelude autenticado ("on"/"off").
DEFERRAL_OVERRIDE_STATE_KEY = "quimera_tool_deferral"

# Conjunto essencial sempre exposto com schema completo. Cobre o ciclo básico
# ler/buscar/editar/executar/perguntar; todo o resto é descoberto via
# tool_search (delegate, git_*, browser_*, host_*, web_*, tasks, memória...).
ESSENTIAL_TOOLS = frozenset({
    "read_file",
    "list_files",
    "grep_search",
    "write_file",
    "replace_text",
    "apply_patch",
    "run_shell",
    "ask_user",
})

TOOL_SEARCH_DEFAULT_RESULTS = 5
TOOL_SEARCH_MAX_RESULTS = 10
_CATALOG_LINE_MAX_CHARS = 110

_TRUE_OVERRIDES = frozenset({"on", "1", "true"})
_FALSE_OVERRIDES = frozenset({"off", "0", "false"})


def _schema_name(schema: Mapping[str, Any]) -> str:
    function = schema.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "")
    return ""


def _schema_description(schema: Mapping[str, Any]) -> str:
    function = schema.get("function")
    if isinstance(function, Mapping):
        return str(function.get("description") or "")
    return ""


def normalized_tool_deferral_mode(config: Any) -> str:
    """Modo efetivo de deferral; valores desconhecidos degradam para "off"."""
    mode = str(getattr(config, "tool_deferral", "off") or "off").strip().lower()
    return mode if mode in TOOL_DEFERRAL_MODES else "off"


def is_api_tool_deferral_enabled(config: Any) -> bool:
    """Deferral ativo para drivers que montam a própria requisição (API)."""
    return normalized_tool_deferral_mode(config) in {"api", "all"}


def is_mcp_tool_deferral_enabled(config: Any, state: Mapping[str, Any] | None) -> bool:
    """Deferral ativo para uma conexão MCP, honrando o override do prelude."""
    override = str((state or {}).get(DEFERRAL_OVERRIDE_STATE_KEY) or "").strip().lower()
    if override in _TRUE_OVERRIDES:
        return True
    if override in _FALSE_OVERRIDES:
        return False
    return normalized_tool_deferral_mode(config) == "all"


def catalog_line(schema: Mapping[str, Any]) -> str:
    """Linha de catálogo "nome: resumo" para uma tool diferida."""
    name = _schema_name(schema)
    description = _schema_description(schema).strip()
    first_sentence = description.split(". ", 1)[0].strip().rstrip(".")
    if len(first_sentence) > _CATALOG_LINE_MAX_CHARS:
        first_sentence = first_sentence[: _CATALOG_LINE_MAX_CHARS - 1].rstrip() + "…"
    return f"- {name}: {first_sentence}" if first_sentence else f"- {name}"


def _decorate_tool_search(search_schema: dict, deferred: list[dict]) -> dict:
    decorated = deepcopy(search_schema)
    catalog = "\n".join(catalog_line(schema) for schema in deferred)
    function = decorated.setdefault("function", {})
    base_description = str(function.get("description") or "").strip()
    function["description"] = (
        f"{base_description}\n\nFerramentas diferidas disponíveis agora:\n{catalog}"
    )
    return decorated


def build_exposed_schemas(
    schemas: list[dict],
    enabled_names: Iterable[str] | None,
) -> list[dict]:
    """Visão exposta ao modelo: essenciais + habilitadas + tool_search com catálogo.

    Se ``tool_search`` não estiver no catálogo resolvido (removida por
    allowlist/denylist), não há caminho de descoberta e o deferral é
    desativado devolvendo os schemas intactos.
    """
    search_schema: dict | None = None
    for schema in schemas:
        if _schema_name(schema) == TOOL_SEARCH_TOOL_NAME:
            search_schema = schema
            break
    if search_schema is None:
        return list(schemas)

    enabled = {str(name) for name in (enabled_names or ())}
    exposed: list[dict] = []
    deferred: list[dict] = []
    for schema in schemas:
        name = _schema_name(schema)
        if name == TOOL_SEARCH_TOOL_NAME:
            continue
        if name in ESSENTIAL_TOOLS or name in enabled:
            exposed.append(schema)
        else:
            deferred.append(schema)
    if deferred:
        exposed.append(_decorate_tool_search(search_schema, deferred))
    return exposed


def _query_tokens(query: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9_]+", query.lower()) if token]


def _score_schema(schema: Mapping[str, Any], tokens: list[str]) -> int:
    name = _schema_name(schema).lower()
    description = _schema_description(schema).lower()
    score = 0
    for token in tokens:
        if token in name:
            score += 3
        if token in description:
            score += 1
    return score


def match_deferred_schemas(
    schemas: list[dict],
    *,
    names: Iterable[str] | None = None,
    query: str | None = None,
    max_results: int = TOOL_SEARCH_DEFAULT_RESULTS,
) -> dict[str, list]:
    """Resolve o pedido de tool_search contra o catálogo completo.

    Retorna dict com:
      - ``matched``: schemas completos encontrados (habilitados + já expostos)
      - ``enabled``: nomes diferidos que passam a ficar habilitados
      - ``already_available``: nomes pedidos que já eram essenciais
      - ``unknown``: nomes pedidos que não existem no catálogo
    """
    by_name = {_schema_name(schema): schema for schema in schemas}
    by_name.pop("", None)

    matched: list[dict] = []
    enabled: list[str] = []
    already_available: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()

    for raw_name in names or ():
        name = str(raw_name).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        schema = by_name.get(name)
        if schema is None:
            unknown.append(name)
            continue
        matched.append(schema)
        if name in ESSENTIAL_TOOLS or name == TOOL_SEARCH_TOOL_NAME:
            already_available.append(name)
        else:
            enabled.append(name)

    tokens = _query_tokens(query or "")
    if tokens:
        limit = max(1, min(int(max_results), TOOL_SEARCH_MAX_RESULTS))
        candidates = [
            (score, name, schema)
            for name, schema in by_name.items()
            if name not in seen
            and name != TOOL_SEARCH_TOOL_NAME
            and name not in ESSENTIAL_TOOLS
            and (score := _score_schema(schema, tokens)) > 0
        ]
        candidates.sort(key=lambda item: (-item[0], item[1]))
        for _score, name, schema in candidates[:limit]:
            seen.add(name)
            matched.append(schema)
            enabled.append(name)

    return {
        "matched": matched,
        "enabled": enabled,
        "already_available": already_available,
        "unknown": unknown,
    }


__all__ = [
    "DEFERRAL_OVERRIDE_STATE_KEY",
    "DEFERRAL_STATE_KEY",
    "ESSENTIAL_TOOLS",
    "TOOL_DEFERRAL_MODES",
    "TOOL_SEARCH_DEFAULT_RESULTS",
    "TOOL_SEARCH_MAX_RESULTS",
    "TOOL_SEARCH_TOOL_NAME",
    "build_exposed_schemas",
    "catalog_line",
    "is_api_tool_deferral_enabled",
    "is_mcp_tool_deferral_enabled",
    "match_deferred_schemas",
    "normalized_tool_deferral_mode",
]
