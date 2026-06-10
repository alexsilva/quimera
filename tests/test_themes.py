"""Testes de cobertura para quimera/themes.py."""
import importlib
import sys
import unittest
from unittest.mock import MagicMock


class ThemesRenderFunctionsTest(unittest.TestCase):
    """Garante que todas as funções de renderização e Theme.render() são exercitadas."""

    def _console(self):
        return MagicMock()

    def test_render_panel(self):
        """Verifica que _render_panel chama console.print."""
        from quimera.themes import _render_panel
        console = self._console()
        _render_panel(console, "Claude", "cyan", "content")
        self.assertTrue(console.print.called)

    def test_render_chat(self):
        """Verifica que _render_chat chama console.print."""
        from quimera.themes import _render_chat
        console = self._console()
        _render_chat(console, "Claude", "cyan", "content")
        self.assertTrue(console.print.called)

    def test_render_rule(self):
        """Verifica que _render_rule imprime 4 vezes (blank + top rule + content + bottom rule)."""
        from quimera.themes import _render_rule
        console = self._console()
        _render_rule(console, "Claude", "cyan", "content")
        # blank + top rule + content + bottom rule
        self.assertEqual(console.print.call_count, 4)

    def test_render_minimal(self):
        """Verifica que _render_minimal imprime 3 vezes (blank + label + content)."""
        from quimera.themes import _render_minimal
        console = self._console()
        _render_minimal(console, "Claude", "cyan", "content")
        # blank + label, content
        self.assertEqual(console.print.call_count, 3)

    def test_render_card(self):
        """Verifica que _render_card chama console.print."""
        from quimera.themes import _render_card
        console = self._console()
        _render_card(console, "Claude", "cyan", "content")
        self.assertTrue(console.print.called)

    def test_render_line(self):
        """Verifica que _render_line chama console.print."""
        from quimera.themes import _render_line
        console = self._console()
        _render_line(console, "Claude", "cyan", "content")
        self.assertTrue(console.print.called)

    def test_theme_render_delegates_to_fn(self):
        """Verifica que Theme.render delega para a função de renderização."""
        from quimera.themes import Theme
        fn = MagicMock()
        theme = Theme(name="test", description="desc", render_fn=fn)
        console = self._console()
        theme.render(console, "Agent", "blue", "md")
        fn.assert_called_once_with(console, "Agent", "blue", "md")

    def test_get_returns_correct_theme(self):
        """Verifica que get retorna o tema pelo nome."""
        import quimera.themes as themes
        theme = themes.get("panel")
        self.assertEqual(theme.name, "panel")

    def test_get_falls_back_to_default(self):
        """Verifica que get retorna o tema padrão para nome inexistente."""
        import quimera.themes as themes
        theme = themes.get("nonexistent_xyz")
        self.assertEqual(theme.name, themes.DEFAULT_THEME)

    def test_names_returns_all_themes(self):
        """Verifica que names retorna lista com todos os temas disponíveis."""
        import quimera.themes as themes
        names = themes.names()
        self.assertIsInstance(names, list)
        for key in ("panel", "chat", "rule", "minimal", "card", "line"):
            self.assertIn(key, names)

    def test_rich_unavailable_sets_flag_false(self):
        """Ao recarregar o módulo sem rich, _RICH_AVAILABLE deve ser False."""
        import quimera.themes as themes_mod

        blocked = {k: None for k in list(sys.modules.keys()) if k == "rich" or k.startswith("rich.")}
        blocked.update({
            "rich": None,
            "rich.console": None,
            "rich.markdown": None,
            "rich.padding": None,
            "rich.panel": None,
            "rich.rule": None,
            "rich.table": None,
            "rich.text": None,
        })
        with unittest.mock.patch.dict(sys.modules, blocked):
            reloaded = importlib.reload(themes_mod)
            flag = reloaded._RICH_AVAILABLE
        # Restore module to normal (rich available)
        importlib.reload(themes_mod)
        self.assertFalse(flag)


if __name__ == "__main__":
    unittest.main()
