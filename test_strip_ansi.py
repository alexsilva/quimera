#!/usr/bin/env python3
"""Teste da função strip_ansi."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

from quimera.ui import strip_ansi

def test_strip_ansi():
    # Teste com sequências ANSI reais
    ansi_text = "\x1b[1mBold\x1b[0m e \x1b[31mRed\x1b[0m texto"
    result = strip_ansi(ansi_text)
    expected = "Bold e Red texto"
    assert result == expected, f"Esperado '{expected}', got '{result}'"
    
    # Teste com sequências ANSI órfãs (sem \x1b)
    orphaned_text = "[1mBold[0m e [?25h[1G[2K texto"
    result = strip_ansi(orphaned_text)
    expected = "Bold e  texto"  # [1m, [0m, [?25h, [1G, [2K devem ser removidos
    assert result == expected, f"Esperado '{expected}', got '{result}'"
    
    # Teste com markup Rich (não deve ser removido)
    rich_text = "[bold]Bold[/bold] e [dim]Dim[/dim] texto"
    result = strip_ansi(rich_text)
    expected = rich_text  # Deve permanecer inalterado
    assert result == expected, f"Esperado '{expected}', got '{result}'"
    
    # Teste misto: ANSI + Rich
    mixed_text = "\x1b[1m[bold]Bold[/bold]\x1b[0m e [dim]Dim[/dim]"
    result = strip_ansi(mixed_text)
    expected = "[bold]Bold[/bold] e [dim]Dim[/dim]"
    assert result == expected, f"Esperado '{expected}', got '{result}'"
    
    print("Todos os testes passaram!")

if __name__ == "__main__":
    test_strip_ansi()