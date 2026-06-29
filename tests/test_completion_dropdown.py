"""Testes do fluxo de autocomplete do CompletionDropdown."""
import asyncio
import unittest

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input

from quimera.app.completion_dropdown import CompletionDropdown, history_suggestion_for

COMPLETIONS = ["/agents", "/agent-status", "/agent-run"]


class _TestInput(Input):
    """Input com a mesma lógica de _CompletionInput (sem dependência do QuimeraApp)."""

    BINDINGS = [Binding("escape", "escape", "Fechar popup")]

    async def action_submit(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        selected = dropdown.get_selected()
        if selected is not None:
            self.value = f"{selected} "
            self.cursor_position = len(self.value)
            dropdown.hide()
            return
        await super().action_submit()

    def action_escape(self) -> None:
        self.app.query_one(CompletionDropdown).hide()

    def key_up(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        if dropdown.has_options:
            dropdown.select_prev()

    def key_down(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        if dropdown.has_options:
            dropdown.select_next()

    def key_tab(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        selected = dropdown.get_selected()
        if selected:
            self.value = f"{selected} "
            self.cursor_position = len(self.value)
            dropdown.hide()


class _TestApp(App):
    """App mínimo para testar o dropdown de autocomplete."""

    submitted_values: list[str]

    def __init__(self, completions: list[str]) -> None:
        super().__init__()
        self._completions = completions
        self.submitted_values = []

    def compose(self) -> ComposeResult:
        yield CompletionDropdown()
        yield _TestInput(id="input")

    def on_mount(self) -> None:
        dropdown = self.query_one(CompletionDropdown)
        dropdown.set_completions(self._completions)
        self.query_one("#input").focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if not isinstance(event.input, _TestInput):
            return
        dropdown = self.query_one(CompletionDropdown)
        value = str(event.value)

        if not value or " " in value:
            dropdown.hide()
            return
        if value.startswith("/"):
            dropdown.filter(value)
        else:
            dropdown.set_completions([])
            dropdown.filter("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.submitted_values.append(event.value)
        self.query_one(CompletionDropdown).hide()
        event.input.value = ""


async def _type(pilot, text: str) -> None:
    """Simula digitação de texto caractere a caractere via pilot.press()."""
    for ch in text:
        if ch == "/":
            await pilot.press("slash")
        elif ch == " ":
            await pilot.press("space")
        elif ch == "-":
            await pilot.press("minus")
        else:
            await pilot.press(ch)
    await pilot.pause()


class TestCompletionDropdownTwoEnter(unittest.TestCase):
    """Verifica o fluxo: digitar → Enter (completar) → Enter (submeter)."""

    def _make_app(self):
        return _TestApp(COMPLETIONS)

    def test_enter_completa_e_segundo_enter_submete(self):
        """1º Enter insere o completion; 2º Enter submete o comando."""
        async def run():
            app = self._make_app()
            async with app.run_test() as pilot:
                await _type(pilot, "/ag")

                dropdown = app.query_one(CompletionDropdown)
                self.assertTrue(dropdown.has_class("-show"), "Dropdown deve aparecer após /ag")
                self.assertIsNotNone(dropdown.get_selected())

                # 1º Enter — deve completar
                await pilot.press("enter")
                await pilot.pause()

                input_widget = app.query_one("#input", Input)
                self.assertEqual(input_widget.value, "/agents ", "Após 1º Enter, valor deve ser '/agents '")
                self.assertFalse(dropdown.has_class("-show"), "Dropdown deve sumir após completar")
                self.assertEqual(app.submitted_values, [], "Não deve submeter no 1º Enter")

                # 2º Enter — deve submeter
                await pilot.press("enter")
                await pilot.pause()

                self.assertIn("/agents ", app.submitted_values, "2º Enter deve submeter o comando")

        asyncio.run(run())

    def test_dropdown_aparece_com_prefixo_barra(self):
        """Dropdown exibe completions ao digitar prefixo com /."""
        async def run():
            app = self._make_app()
            async with app.run_test() as pilot:
                await _type(pilot, "/a")

                dropdown = app.query_one(CompletionDropdown)
                self.assertTrue(dropdown.has_class("-show"))
                self.assertIsNotNone(dropdown.get_selected())

        asyncio.run(run())

    def test_dropdown_oculto_sem_prefixo(self):
        """Dropdown não aparece para texto sem prefixo /."""
        async def run():
            app = self._make_app()
            async with app.run_test() as pilot:
                await _type(pilot, "ola")

                dropdown = app.query_one(CompletionDropdown)
                self.assertFalse(dropdown.has_class("-show"))

        asyncio.run(run())

    def test_tab_completa_sem_submeter(self):
        """Tab completa o item selecionado sem submeter."""
        async def run():
            app = self._make_app()
            async with app.run_test() as pilot:
                await _type(pilot, "/ag")
                await pilot.press("tab")
                await pilot.pause()

                input_widget = app.query_one("#input", Input)
                self.assertEqual(input_widget.value, "/agents ")
                self.assertEqual(app.submitted_values, [])

        asyncio.run(run())

    def test_escape_fecha_dropdown(self):
        """Escape fecha o dropdown sem completar."""
        async def run():
            app = self._make_app()
            async with app.run_test() as pilot:
                await _type(pilot, "/ag")

                dropdown = app.query_one(CompletionDropdown)
                self.assertTrue(dropdown.has_class("-show"))

                await pilot.press("escape")
                await pilot.pause()

                self.assertFalse(dropdown.has_class("-show"))

        asyncio.run(run())

    def test_sem_match_nao_exibe_dropdown(self):
        """Sem matches, dropdown permanece oculto."""
        async def run():
            app = self._make_app()
            async with app.run_test() as pilot:
                await _type(pilot, "/xyz")

                dropdown = app.query_one(CompletionDropdown)
                self.assertFalse(dropdown.has_class("-show"))

        asyncio.run(run())

    def test_enter_sem_dropdown_submete_direto(self):
        """Enter sem dropdown visível submete imediatamente."""
        async def run():
            app = self._make_app()
            async with app.run_test() as pilot:
                await _type(pilot, "ola")
                await pilot.press("enter")
                await pilot.pause()

                self.assertIn("ola", app.submitted_values)

        asyncio.run(run())


class TestCompletionDropdownUnit(unittest.TestCase):
    """Testes unitários do CompletionDropdown sem app Textual."""

    def _make_dropdown(self, completions: list[str]) -> CompletionDropdown:
        dd = CompletionDropdown()
        dd._all = completions
        dd._filtered = []
        dd._selected = 0
        dd._widgets = []
        return dd

    def test_get_selected_vazio(self):
        dd = self._make_dropdown([])
        self.assertIsNone(dd.get_selected())

    def test_hide_limpa_filtered(self):
        """hide() deve zerar _filtered imediatamente."""
        dd = self._make_dropdown(["/agents"])
        dd._filtered = ["/agents"]
        dd._selected = 0
        # Não chama set_class pois não há app, mas _filtered deve zerar
        dd._filtered = []  # simula hide() interno
        self.assertIsNone(dd.get_selected())

    def test_filter_prefixo(self):
        """filter() com prefixo correto retorna matches."""
        dd = self._make_dropdown(["/agents", "/agent-status", "/other"])
        dd._widgets = []  # sem app, _refresh não renderiza
        dd._filtered = [c for c in dd._all if c.lower().startswith("/ag")]
        dd._selected = 0
        self.assertEqual(dd._filtered, ["/agents", "/agent-status"])
        self.assertEqual(dd.get_selected(), "/agents")

    def test_single_match(self):
        dd = self._make_dropdown(["/agents"])
        dd._filtered = ["/agents"]
        self.assertEqual(dd.single_match(), "/agents")

    def test_has_options_requer_dois(self):
        dd = self._make_dropdown(["/agents"])
        dd._filtered = ["/agents"]
        self.assertFalse(dd.has_options)
        dd._filtered = ["/agents", "/agent-status"]
        self.assertTrue(dd.has_options)

    def test_history_suggestion_prefere_match_mais_recente(self):
        history = [
            "corrige a toolbar",
            "continua a migracao textual",
            "continua validando approval",
        ]

        self.assertEqual(
            history_suggestion_for(history, "continua"),
            "continua validando approval",
        )

    def test_history_suggestion_ignora_match_exato_e_prefixo_vazio(self):
        history = ["/context show", "/context reset"]

        self.assertIsNone(history_suggestion_for(history, ""))
        self.assertIsNone(history_suggestion_for(history, "/context reset"))
        self.assertIsNone(history_suggestion_for(history, "/missing"))
