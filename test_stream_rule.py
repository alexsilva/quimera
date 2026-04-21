from quimera.ui import TerminalRenderer

def test_rule_stream():
    renderer = TerminalRenderer(theme="rule")
    renderer.start_message_stream("test_agent")
    renderer.update_message_stream("test_agent", "Conteúdo chunk 1 ")
    renderer.update_message_stream("test_agent", "Conteúdo chunk 2 ")
    renderer.finish_message_stream("test_agent", "Conteúdo chunk 1 Conteúdo chunk 2")

if __name__ == "__main__":
    test_rule_stream()