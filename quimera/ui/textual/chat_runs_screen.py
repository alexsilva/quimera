"""Menu dos slots de chat em execução."""
from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label


class ChatRunsScreen(ModalScreen[None]):
    """Lista prompts ativos/na fila e permite parar somente o selecionado.

    Abre pelo clique no contador ``⚡`` da toolbar, por ``F7`` ou pela paleta de
    comandos; ``F7`` e ``Esc`` fecham (``F8`` já cola o clipboard no input).
    """

    CSS = """
    ChatRunsScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }
    #chat_runs_dialog {
        width: 94%;
        max-width: 112;
        height: 70%;
        max-height: 24;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    #chat_runs_header {
        height: 1;
        margin-bottom: 1;
    }
    #chat_runs_title {
        width: 1fr;
        content-align: center middle;
        text-align: center;
        text-style: bold;
        color: $accent;
    }
    #chat_runs_close_top {
        width: 3;
        min-width: 3;
        height: 1;
        border: none;
        padding: 0;
    }
    #chat_runs_summary {
        height: 1;
        color: $text-muted;
        margin-bottom: 1;
    }
    #chat_runs_table {
        height: 1fr;
    }
    #chat_runs_footer {
        height: 2;
        margin-top: 1;
        align-horizontal: right;
    }
    #chat_runs_footer Button {
        height: 1;
        min-width: 18;
        border: none;
        margin-left: 1;
    }
    #chat_runs_message {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Fechar"),
        ("f7", "close", "Fechar"),
        ("s", "stop_selected", "Parar selecionado"),
    ]
    AUTO_FOCUS = "#chat_runs_table"
    COLUMNS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("slot", "Slot"),
        ("agent", "Agente"),
        ("status", "Estado"),
        ("elapsed", "Tempo"),
        ("prompt", "Pedido"),
    )

    def __init__(self, quimera_app, parent_app) -> None:
        super().__init__()
        self.quimera_app = quimera_app
        self.parent_app = parent_app
        self._selected_submission_id: str | None = None
        self._executions_by_id: dict[str, object] = {}
        self._refresh_timer: object | None = None

    def compose(self) -> ComposeResult:
        with Container(id="chat_runs_dialog"):
            with Horizontal(id="chat_runs_header"):
                yield Label("Execuções do chat", id="chat_runs_title")
                yield Button("×", id="chat_runs_close_top")
            yield Label("", id="chat_runs_summary")
            yield DataTable(
                id="chat_runs_table",
                cursor_type="row",
                zebra_stripes=True,
            )
            with Horizontal(id="chat_runs_footer"):
                yield Label("", id="chat_runs_message")
                yield Button(
                    "Parar selecionado",
                    id="chat_runs_stop",
                    variant="error",
                    disabled=True,
                )
                yield Button("Fechar", id="chat_runs_close")

    def on_mount(self) -> None:
        table = self.query_one("#chat_runs_table", DataTable)
        for key, label in self.COLUMNS:
            table.add_column(label, key=key)
        self._refresh_view()
        self._refresh_timer = self.set_interval(0.5, self._refresh_view)

    def on_unmount(self) -> None:
        timer = self._refresh_timer
        self._refresh_timer = None
        stop = getattr(timer, "stop", None)
        if callable(stop):
            stop()

    def _list_executions(self) -> list:
        lifecycle = getattr(self.quimera_app, "chat_lifecycle", None)
        provider = getattr(lifecycle, "list_chat_executions", None)
        return list(provider() or ()) if callable(provider) else []

    def _slot_counts(self) -> tuple[int, int, int]:
        runtime_state = getattr(self.quimera_app, "runtime_state", None)
        get_active = getattr(runtime_state, "get_chat_inflight_count", None)
        get_pending = getattr(runtime_state, "get_chat_pending_count", None)
        active = int(get_active() or 0) if callable(get_active) else 0
        pending = int(get_pending() or 0) if callable(get_pending) else 0
        capacity = max(0, int(getattr(self.quimera_app, "threads", 0) or 0))
        return active, capacity, pending

    @staticmethod
    def _status_cell(execution) -> Text:
        status = str(getattr(execution, "status", "") or "")
        queue_position = getattr(execution, "queue_position", None)
        if status == "queued" and queue_position:
            return Text(f"fila {queue_position}", style="yellow")
        labels = {
            "accepted": "aceita",
            "queued": "aguardando",
            "starting": "iniciando",
            "running": "executando",
            "stopping": "parando",
        }
        label = labels.get(status, status or "aguardando")
        style = "green" if status == "running" else "yellow" if status == "stopping" else ""
        return Text(label, style=style)

    @staticmethod
    def _prompt_label(prompt: str) -> str:
        compact = " ".join(str(prompt or "").split())
        return f"{compact[:79].rstrip()}…" if len(compact) > 80 else compact

    def _row_cells(self, execution) -> dict[str, object]:
        return {
            "slot": f"#{execution.number}",
            "agent": execution.agent or "—",
            "status": self._status_cell(execution),
            "elapsed": execution.elapsed_label,
            "prompt": self._prompt_label(execution.prompt),
        }

    def _sync_rows(self, table: DataTable, executions: list) -> None:
        """Atualiza a tabela no lugar, linha a linha, pela chave da submission.

        ``DataTable.clear()`` zera a rolagem horizontal e vertical, então
        reconstruir a tabela a cada tick jogava a visão de volta ao início.
        A lista chega em ordem de número e linhas novas entram sempre no fim.
        """
        wanted = {str(item.submission_id) for item in executions}
        for stale in [key for key in table.rows if key.value not in wanted]:
            table.remove_row(stale)
        for execution in executions:
            row_key = str(execution.submission_id)
            cells = self._row_cells(execution)
            if row_key not in table.rows:
                table.add_row(*cells.values(), key=row_key)
                continue
            for column_key, value in cells.items():
                if table.get_cell(row_key, column_key) != value:
                    table.update_cell(row_key, column_key, value, update_width=True)

    @staticmethod
    def _cursor_submission_id(table: DataTable) -> str | None:
        if not table.row_count:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value or "") or None

    def _refresh_view(self) -> None:
        executions = self._list_executions()
        self._executions_by_id = {
            str(item.submission_id): item for item in executions
        }
        table = self.query_one("#chat_runs_table", DataTable)
        self._sync_rows(table, executions)

        selected = self._selected_submission_id
        if selected in self._executions_by_id:
            selected_row = table.get_row_index(selected)
            if table.cursor_row != selected_row:
                table.move_cursor(row=selected_row, animate=False)
        else:
            self._selected_submission_id = self._cursor_submission_id(table)

        active, capacity, pending = self._slot_counts()
        summary = f"⚡ {active}/{capacity} slots"
        if pending:
            summary = f"{summary} · {pending} na fila"
        if not executions:
            summary = f"{summary} · nenhuma execução ativa"
        self.query_one("#chat_runs_summary", Label).update(summary)
        self.query_one("#chat_runs_stop", Button).disabled = (
            self._selected_submission_id is None
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value if event.row_key is not None else ""
        self._selected_submission_id = str(key or "") or None
        self.query_one("#chat_runs_stop", Button).disabled = (
            self._selected_submission_id is None
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        highlighted = DataTable.RowHighlighted(
            event.data_table,
            event.cursor_row,
            event.row_key,
        )
        self.on_data_table_row_highlighted(highlighted)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in {"chat_runs_close", "chat_runs_close_top"}:
            self.action_close()
        elif event.button.id == "chat_runs_stop":
            self.action_stop_selected()

    def action_stop_selected(self) -> None:
        submission_id = self._selected_submission_id
        if not submission_id:
            self.query_one("#chat_runs_message", Label).update(
                "Selecione uma execução."
            )
            return
        lifecycle = getattr(self.quimera_app, "chat_lifecycle", None)
        stop = getattr(lifecycle, "stop_chat_submission", None)
        if not callable(stop):
            self.query_one("#chat_runs_message", Label).update(
                "Cancelamento seletivo indisponível."
            )
            return
        result = stop(submission_id)
        self.query_one("#chat_runs_message", Label).update(
            str(getattr(result, "message", result) or "")
        )
        self._selected_submission_id = None
        self._refresh_view()

    def action_close(self) -> None:
        self.dismiss()
