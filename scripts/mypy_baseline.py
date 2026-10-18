"""Compara a baseline de dívida do mypy com o estado real dos módulos.

Roda o mypy com a configuração do pyproject.toml menos os overrides de
``ignore_errors`` e imprime: módulos da baseline que já não têm erro (devem
sair da lista) e módulos com erro fora dela (o gate normal já os bloqueia).
Uso: scripts/check.sh --baseline  (ou: python scripts/mypy_baseline.py <mypy>)
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main(mypy: str) -> int:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mypy_cfg = config["tool"]["mypy"]
    overrides = mypy_cfg.get("overrides", [])
    baseline = {
        module
        for override in overrides
        if override.get("ignore_errors")
        for module in override.get("module", [])
    }
    kept = [o for o in overrides if not o.get("ignore_errors")]
    lines = ["[mypy]"]
    for key, value in mypy_cfg.items():
        if key == "overrides":
            continue
        if isinstance(value, bool):
            lines.append(f"{key} = {str(value)}")
        elif isinstance(value, list):
            lines.append(f"{key} = {','.join(map(str, value))}")
        else:
            lines.append(f"{key} = {value}")
    for override in kept:
        lines.append(f"\n[mypy-{','.join(override['module'])}]")
        for key, value in override.items():
            if key != "module":
                lines.append(f"{key} = {str(value)}")
    with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
        ini = fh.name
    proc = subprocess.run(
        [mypy, "--config-file", ini, "--no-error-summary"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    failing: set[str] = set()
    for line in proc.stdout.splitlines():
        match = re.match(r"^(quimera/[\w/]+)\.py:\d+: error:", line)
        if match:
            failing.add(match.group(1).replace("/", "."))
    clean = sorted(baseline - failing)
    outside = sorted(failing - baseline)
    print(f"baseline: {len(baseline)} módulos; com erro hoje: {len(failing)}")
    if clean:
        print("\nJá limpos (remova de [[tool.mypy.overrides]]):")
        print("\n".join(f"  - {m}" for m in clean))
    if outside:
        print("\nCom erro fora da baseline (o gate normal bloqueia):")
        print("\n".join(f"  - {m}" for m in outside))
    if not clean and not outside:
        print("baseline exata: nada a remover, nada fora dela.")
    return 1 if outside else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "mypy"))
