#!/usr/bin/env bash
# Gates de qualidade do Quimera: Ruff (lint) e mypy (tipos), ambos bloqueantes.
#
# Uso: scripts/check.sh            # tudo
#      scripts/check.sh --lint     # só Ruff
#      scripts/check.sh --types    # só mypy
#      scripts/check.sh --baseline # lento: módulos da baseline do mypy já limpos
#
# O hook scripts/git-hooks/pre-commit roda este script; instale com
#   git config core.hooksPath scripts/git-hooks
# A baseline de módulos com dívida de tipos vive em pyproject.toml
# ([[tool.mypy.overrides]] ignore_errors); ela só pode encolher.
#
# QUIMERA_CHECK_TOOLS_DIR aponta para o diretório das ferramentas (padrão
# .venv/bin relativo ao repositório); o hook usa isso ao verificar uma
# exportação do índice fora da árvore de trabalho.
set -u
cd "$(dirname "$0")/.."
tools_dir="${QUIMERA_CHECK_TOOLS_DIR:-.venv/bin}"

bin() {
    if [ -x "$tools_dir/$1" ]; then
        printf '%s' "$tools_dir/$1"
    else
        command -v "$1" || { echo "check: '$1' não encontrado (pip install -e '.[dev]')" >&2; return 127; }
    fi
}

run_lint=1
run_types=1
case "${1:-}" in
    --lint) run_types=0 ;;
    --types) run_lint=0 ;;
    --baseline)
        mypy=$(bin mypy) || exit 127
        python=$(bin python) || exit 127
        echo "check: mypy sem a baseline (pyproject.toml menos [[tool.mypy.overrides]] ignore_errors)"
        exec "$python" scripts/mypy_baseline.py "$mypy"
        ;;
    "") ;;
    *) echo "uso: $0 [--lint|--types|--baseline]" >&2; exit 2 ;;
esac

status=0
if [ "$run_lint" = 1 ]; then
    ruff=$(bin ruff) || exit 127
    echo "check: ruff check quimera tests"
    "$ruff" check quimera tests || status=1
fi
if [ "$run_types" = 1 ]; then
    mypy=$(bin mypy) || exit 127
    echo "check: mypy (pyproject.toml)"
    "$mypy" || status=1
fi
if [ "$status" != 0 ]; then
    echo "check: FALHOU — corrija antes de commitar (a baseline do mypy não aceita módulos novos)." >&2
fi
exit "$status"
