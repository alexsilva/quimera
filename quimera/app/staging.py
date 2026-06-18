"""Operações de staging merge para arquivos escritos por agentes."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .config import logger

if TYPE_CHECKING:
    from ..workspace import Workspace


def merge_staging_to_workspace(staging_root: Path, workspace: "Workspace") -> list[dict]:
    """Mescla arquivos do staging para o workspace em ordem de índice com auditoria."""
    if not staging_root.exists():
        logger.debug("merge: staging_root does not exist, skipping")
        return []

    workspace_root = workspace.cwd.resolve()
    manifest = []
    index_dirs = sorted(
        staging_root.iterdir(),
        key=lambda p: int(p.name) if p.name.isdigit() else 999,
    )

    for index_dir in index_dirs:
        if not index_dir.is_dir() or index_dir.is_symlink():
            continue
        for src in index_dir.rglob("*"):
            if src.is_symlink():
                raise ValueError(f"merge blocked symlink source: {src}")
            if not src.is_file():
                continue
            rel_path = src.relative_to(index_dir)
            dest = workspace.cwd / rel_path
            dest_resolved = dest.resolve(strict=False)
            if not dest_resolved.is_relative_to(workspace_root):
                raise ValueError(
                    f"merge blocked destination outside workspace: {rel_path}"
                )
            if dest.exists() and dest.is_symlink():
                raise ValueError(f"merge blocked symlink destination: {dest}")
            overwritten = dest.exists()
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source": str(src),
                "destination": str(dest_resolved),
                "relative_path": rel_path.as_posix(),
                "overwritten": overwritten,
            }
            manifest.append(entry)
            logger.info(
                "merge %s: %s -> %s",
                "overwrote" if overwritten else "created",
                src,
                dest_resolved,
            )

    manifest_path = getattr(workspace, "state_dir", None)
    if manifest and manifest_path is not None:
        manifest_file = manifest_path / "staging_merge_manifest.jsonl"
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        with manifest_file.open("a", encoding="utf-8") as fh:
            for entry in manifest:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info("merge completed: %d files to %s", len(manifest), workspace_root)
    return manifest
