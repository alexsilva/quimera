#!/usr/bin/env bash
set -euo pipefail

source_path="${BASH_SOURCE[0]}"
while [[ -L "$source_path" ]]; do
    source_dir="$(cd -P "$(dirname "$source_path")" >/dev/null 2>&1 && pwd)"
    source_path="$(readlink "$source_path")"
    if [[ "$source_path" != /* ]]; then
        source_path="$source_dir/$source_path"
    fi
done

quimera_root="$(cd -P "$(dirname "$source_path")" >/dev/null 2>&1 && pwd)"
python_bin="$quimera_root/.venv/bin/python"
entrypoint="$quimera_root/quimera.py"

if [[ ! -x "$python_bin" ]]; then
    printf 'Quimera virtualenv not found: %s\n' "$python_bin" >&2
    exit 1
fi

if [[ ! -f "$entrypoint" ]]; then
    printf 'Quimera entrypoint not found: %s\n' "$entrypoint" >&2
    exit 1
fi

exec "$python_bin" "$entrypoint" "$@"
