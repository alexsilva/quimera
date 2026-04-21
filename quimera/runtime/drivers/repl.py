"""
Modo REPL interativo para testar drivers openai_compat.

Permite executar um agente Ollama/OpenAI-compat diretamente no terminal,
ver tool calls em tempo real e iterar na API com base nas respostas.

Uso via CLI:
    quimera --driver-repl ollama-qwen
    quimera --driver-repl ollama-gemma4 --working-dir /caminho/do/projeto
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from ...plugins.base import OpenAIConnection
from .openai_compat import OpenAICompatDriver
from ..approval import AutoApprovalHandler, ConsoleApprovalHandler
from ..config import ToolRuntimeConfig
from ..executor import ToolExecutor
from ..models import ToolResult

_SEP = "─" * 60


def _header(text: str) -> None:
    """Executa header."""
    print(f"\n{_SEP}")
    print(f"  {text}")
    print(_SEP)


def _on_tool_call(name: str, args: dict) -> None:
    """Executa on tool call."""
    print(f"\n  ▶ TOOL CALL: {name}")
    for k, v in args.items():
        val = str(v)
        if len(val) > 300:
            val = val[:300] + " …"
        print(f"    {k}: {val}")


def _on_tool_result(result: ToolResult) -> None:
    """Executa on tool result."""
    status = "✓ OK" if result.ok else "✗ ERRO"
    content = result.content or result.error or ""
    if len(content) > 400:
        content = content[:400] + " …"
    print(f"  ◀ TOOL RESULT: {result.tool_name} [{status}]")
    if content:
        for line in content.splitlines()[:10]:
            print(f"    {line}")


def _resolve_plugin_connection(plugin):
    """Resolve a conexão do plugin com fallback para objetos simplificados."""
    resolver = getattr(plugin, "effective_connection", None)
    if callable(resolver):
        connection = resolver()
        if isinstance(connection, OpenAIConnection):
            return connection
    driver = getattr(plugin, "driver", "cli")
    if isinstance(driver, str) and driver != "cli":
        return OpenAIConnection(
            model=getattr(plugin, "model", None) or "gpt-4o",
            base_url=getattr(plugin, "base_url", None) or "https://api.openai.com/v1",
            api_key_env=getattr(plugin, "api_key_env", None) or "OPENAI_API_KEY",
            provider=driver,
            supports_native_tools=getattr(plugin, "supports_tools", True),
        )
    return None


def _resolve_plugin_driver(plugin) -> str:
    """Resolve o driver efetivo com fallback para plugins simplificados."""
    resolver = getattr(plugin, "effective_driver", None)
    if callable(resolver):
        return resolver()
    return str(getattr(plugin, "driver", "cli"))


class DriverRepl:
    """Loop REPL para testar um plugin baseado em openai_compat."""

    def __init__(
        self,
        plugin_name: str,
        working_dir: Optional[Path] = None,
        *,
        get_plugin,
        all_plugins,
    ) -> None:
        """Inicializa uma instância de DriverRepl."""
        plugin = get_plugin(plugin_name)
        if plugin is None:
            compat = [p for p in all_plugins() if isinstance(_resolve_plugin_connection(p), OpenAIConnection)]
            names = ", ".join(p.name for p in compat) or "(nenhum)"
            raise ValueError(
                f"Plugin '{plugin_name}' não encontrado. "
                f"Plugins openai_compat disponíveis: {names}"
            )
        connection = _resolve_plugin_connection(plugin)
        if not isinstance(connection, OpenAIConnection):
            raise ValueError(
                f"Plugin '{plugin_name}' usa driver='{_resolve_plugin_driver(plugin)}', "
                "mas o REPL só suporta driver='openai_compat'."
            )

        self.plugin = plugin
        self.working_dir = (working_dir or Path.cwd()).resolve()
        self._last_connection_signature = None
        self._update_driver()

        rt_config = ToolRuntimeConfig(workspace_root=self.working_dir)
        self._rt_config = rt_config
        self.tool_executor = ToolExecutor(rt_config, ConsoleApprovalHandler())
        self._auto_tool_executor = ToolExecutor(rt_config, AutoApprovalHandler(approve_all=True))

    @property
    def connection(self) -> OpenAIConnection:
        """Obtém a conexão atual do plugin, considerando overrides."""
        return self._get_current_connection()

    def _get_current_connection(self) -> OpenAIConnection:
        """Obtém a conexão atual do plugin, considerando overrides."""
        connection = _resolve_plugin_connection(self.plugin)
        if not isinstance(connection, OpenAIConnection):
            raise ValueError(
                f"Plugin '{self.plugin.name}' usa driver='{_resolve_plugin_driver(self.plugin)}', "
                "mas o REPL só suporta driver='openai_compat'."
            )
        return connection

    def _connection_has_changed(self) -> bool:
        """Verifica se a conexão mudou desde a última verificação."""
        current_conn = self._get_current_connection()
        # Criar uma assinatura simples baseada nos campos que afetam o driver
        signature = (
            current_conn.model,
            current_conn.base_url,
            current_conn.api_key_env,
            # Também considerar o valor real da api_key se api_key_env estiver definida
            os.environ.get(current_conn.api_key_env, "") if current_conn.api_key_env else ""
        )
        changed = signature != self._last_connection_signature
        self._last_connection_signature = signature
        return changed

    def _update_driver(self) -> None:
        """Atualiza o driver com a conexão atual."""
        connection = self._get_current_connection()
        api_key = "ollama"
        if connection.api_key_env:
            api_key = os.environ.get(connection.api_key_env, "")
            if not api_key:
                print(
                    f"[aviso] Variável de ambiente '{connection.api_key_env}' não definida. "
                    "Usando string vazia como api_key.",
                    file=sys.stderr,
                )

        self.driver = OpenAICompatDriver(
            model=connection.model,
            base_url=connection.base_url,
            api_key=api_key,
            tool_use_reliability=getattr(self.plugin, "tool_use_reliability", "medium"),
        )

    def _probe_url(self) -> str:
        """Executa probe url."""
        return self.connection.base_url.rstrip("/") + "/models"

    def ensure_backend_available(self, timeout: float = 2.0) -> None:
        """Executa ensure backend available."""
        probe_url = self._probe_url()
        request = urllib_request.Request(probe_url, method="GET")
        try:
            with urllib_request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if 200 <= status < 500:
                    return
                raise RuntimeError(
                    f"Backend do plugin '{self.plugin.name}' respondeu com status HTTP {status} em {probe_url}."
                )
        except urllib_error.HTTPError as exc:
            if 200 <= exc.code < 500:
                return
            raise RuntimeError(
                f"Backend do plugin '{self.plugin.name}' respondeu com status HTTP {exc.code} em {probe_url}."
            ) from exc
        except (urllib_error.URLError, OSError) as exc:
            raise RuntimeError(
                f"Backend do plugin '{self.plugin.name}' indisponível em {probe_url}. "
                "Verifique se o serviço está em execução e acessível."
            ) from exc

    def probe(self, prompt: str, use_tools: bool = True) -> str | None:
        """
        Executa um único prompt e retorna a resposta.
        Útil para uso programático (ex: Claude Code analisando o output).
        """
        self.ensure_backend_available()
        executor = self.tool_executor if use_tools else None
        return self.driver.run(
            prompt,
            tool_executor=executor,
            on_tool_call=_on_tool_call,
            on_tool_result=_on_tool_result,
        )

    def run(self, one_shot_prompt: str | None = None) -> None:
        """Executa run."""
        self.ensure_backend_available()
        print(f"\n{'=' * 60}")
        print(f"  Driver REPL  •  {self.plugin.name}")
        print(f"  Modelo : {self.connection.model}")
        print(f"  URL    : {self.connection.base_url}")
        print(f"  Dir    : {self.working_dir}")
        print(f"{'=' * 60}")

        if one_shot_prompt is not None:
            print(f"  [modo one-shot]\n")
            result = self.probe(one_shot_prompt)
            print(f"\n{_SEP}")
            print(result if result else "[sem resposta]")
            print()
            return

        print("  Comandos especiais:")
        print("    /sem-tools  — próxima mensagem sem ferramentas")
        print("    /tools      — reabilita ferramentas")
        print("    /info       — mostra configuração atual")
        print("    exit / sair — encerra")
        print(f"{'=' * 60}\n")

        use_tools = True

        while True:
            try:
                raw = input(">>> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nSaindo.")
                break

            if not raw:
                continue

            if raw.lower() in {"exit", "quit", "sair"}:
                break

            if raw == "/sem-tools":
                use_tools = False
                print("  [ferramentas desativadas para a próxima mensagem]")
                continue

            if raw == "/tools":
                use_tools = True
                print("  [ferramentas reativadas]")
                continue

            if raw == "/info":
                print(f"  plugin      : {self.plugin.name}")
                print(f"  modelo      : {self._get_current_connection().model}")
                print(f"  base_url    : {self._get_current_connection().base_url}")
                print(f"  working_dir : {self.working_dir}")
                print(f"  ferramentas : {'sim' if use_tools else 'não'}")
                continue

            if raw == "/reload":
                self._update_driver()
                print(f"  [driver recarregado: {self.plugin.name} -> {self.connection.base_url}]")
                continue

            if self._connection_has_changed():
                self._update_driver()
                print(f"  [conexão alterada detectada, driver atualizado]")
                print(f"  [{self.connection.base_url}]")

            executor = self.tool_executor if use_tools else None
            use_tools = True  # reset após cada mensagem com /sem-tools

            print()
            result = self.driver.run(
                raw,
                tool_executor=executor,
                on_tool_call=_on_tool_call,
                on_tool_result=_on_tool_result,
            )

            print(f"\n{_SEP}")
            if result:
                print(result)
            else:
                print("[sem resposta]")
            print()
