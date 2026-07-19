"""Estilos CSS da aplicação Textual."""
from __future__ import annotations

TEXTUAL_APP_CSS = """
        Screen {
            layout: vertical;
            background: $surface;
        }
        ToastRack {
            dock: top;
            align: right top;
            margin-top: 1;
            margin-bottom: 0;
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
            overflow-x: auto;
        }
        #feed_transient {
            display: none;
            height: auto;
            max-height: 10;
            padding: 0 1;
            background: $surface;
        }
        #toolbar {
            height: 1;
            padding: 0 1;
            color: $text;
            background: #1a1a1a;
        }
        #status_bar {
            display: none;
            height: 0;
            padding: 0;
        }
        #question_overlay {
            display: none;
            height: auto;
            max-height: 12;
            overflow-y: auto;
            padding: 0 1;
            background: $surface;
        }
        #agent_status {
            display: none;
            height: 0;
            padding: 0;
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
        #breadcrumb {
            height: 1;
            padding: 0 1;
            color: $text-muted;
            max-width: 50%;
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            content-align: left middle;
        }
        #summary-spinner {
            dock: right;
            width: 3;
            color: $warning;
            content-align: center middle;
            margin-right: 10;
        }
        HeaderClock {
            width: 10;
            padding: 0 1;
        }
        """
