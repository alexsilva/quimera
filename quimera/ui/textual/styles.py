"""Estilos CSS da aplicação Textual."""
from __future__ import annotations

TEXTUAL_APP_CSS = """
        Screen {
            layout: vertical;
            background: $surface;
        }
        #main {
            height: 1fr;
            min-height: 14;
        }
        #feed {
            height: 1fr;
            min-height: 10;
            padding: 0 1;
            background: $background;
        }
        #toolbar {
            height: 1;
            padding: 0 1;
            color: $text;
            background: #1a1a1a;
        }
        #question_overlay {
            display: none;
            height: auto;
            max-height: 12;
            overflow-y: auto;
            padding: 0 1;
            background: $surface;
        }
        #input_bar {
            height: 3;
            max-height: 3;
            padding: 0 1;
            background: $surface;
            align: left middle;
        }
        #input {
            width: 1fr;
        }
        #summary-spinner {
            dock: right;
            width: 3;
            color: $warning;
            content-align: center middle;
        }
        HeaderClock {
            width: 10;
            padding: 0 1;
        }
        """
