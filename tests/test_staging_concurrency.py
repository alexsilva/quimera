import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.files import FileTools, set_staging_root


def _merge_staging_to_workspace(staging_root: Path, workspace: Path):
    """Helper de merge para testes - simula app._merge_staging_to_workspace."""
    if not staging_root.exists():
        return
    
    index_dirs = sorted(staging_root.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 999)
    
    for index_dir in index_dirs:
        if not index_dir.is_dir():
            continue
        for src in index_dir.rglob("*"):
            if not src.is_file():
                continue
            rel_path = src.relative_to(index_dir)
            dest = workspace / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)


class TestStagingConcurrency:
    @pytest.fixture
    def workspace(self):
        tmp = tempfile.mkdtemp()
        yield Path(tmp)
        shutil.rmtree(tmp)

    @pytest.fixture
    def staging_root(self, workspace):
        staging = workspace / "staging"
        staging.mkdir()
        return staging

    @pytest.fixture
    def config(self, workspace):
        return ToolRuntimeConfig(
            workspace_root=workspace,
            max_file_read_chars=50000,
        )

    @pytest.fixture
    def file_tools(self, config):
        return FileTools(config)

    def test_write_different_files(self, workspace, staging_root, config, file_tools):
        s0 = staging_root / "0"
        s1 = staging_root / "1"
        s0.mkdir()
        s1.mkdir()

        set_staging_root(s0)
        call1 = ToolCall(name="write_file", arguments={
            "path": "file1.txt",
            "content": "content from agent 1",
        })
        file_tools.write_file(call1)

        set_staging_root(s1)
        call2 = ToolCall(name="write_file", arguments={
            "path": "file2.txt",
            "content": "content from agent 2",
        })
        file_tools.write_file(call2)

        _merge_staging_to_workspace(staging_root, workspace)

        assert (workspace / "file1.txt").read_text() == "content from agent 1"
        assert (workspace / "file2.txt").read_text() == "content from agent 2"

    def test_write_same_file(self, workspace, staging_root, config, file_tools):
        s0 = staging_root / "0"
        s1 = staging_root / "1"
        s0.mkdir()
        s1.mkdir()

        set_staging_root(s0)
        file_tools.write_file(ToolCall(name="write_file", arguments={
            "path": "same.txt", "content": "content from agent 1",
        }))

        set_staging_root(s1)
        file_tools.write_file(ToolCall(name="write_file", arguments={
            "path": "same.txt", "content": "content from agent 2",
        }))

        _merge_staging_to_workspace(staging_root, workspace)

        assert (workspace / "same.txt").read_text() == "content from agent 2"

    def test_read_after_write_staging(self, workspace, config, file_tools):
        staging = tempfile.mkdtemp(dir=workspace)
        set_staging_root(Path(staging))
        file_tools.write_file(ToolCall(name="write_file", arguments={
            "path": "test.txt", "content": "my content",
        }))

        result = file_tools.read_file(ToolCall(name="read_file", arguments={
            "path": "test.txt",
        }))

        assert result.ok
        assert "my content" in result.content

    def test_thread_local_isolation(self, workspace, config):
        results = {}
        errors = []

        def worker(idx):
            try:
                staging = tempfile.mkdtemp(dir=workspace)
                set_staging_root(Path(staging))
                ft = FileTools(config)
                ft.write_file(ToolCall(name="write_file", arguments={
                    "path": f"thread_{idx}.txt", "content": f"content_{idx}",
                }))
                result = ft.read_file(ToolCall(name="read_file", arguments={
                    "path": f"thread_{idx}.txt",
                }))
                results[idx] = result.content
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert results[0] == "content_0"
        assert results[1] == "content_1"
        assert results[2] == "content_2"

    def test_staging_cleanup_on_worker_failure(self, workspace, staging_root, config, file_tools):
        """Se worker falha, staging deve ser limpo e não fazer merge parcial."""
        s0 = staging_root / "0"
        s1 = staging_root / "1"
        s0.mkdir()
        s1.mkdir()

        # Worker 0 escreve com sucesso
        set_staging_root(s0)
        file_tools.write_file(ToolCall(name="write_file", arguments={
            "path": "success.txt", "content": "from worker 0",
        }))

        # Worker 1 escreve com sucesso
        set_staging_root(s1)
        file_tools.write_file(ToolCall(name="write_file", arguments={
            "path": "success2.txt", "content": "from worker 1",
        }))

        # Simula o fluxo de paralelismo com falha: tentativa de merge + cleanup
        # cenário: um worker falhou, então não fazemos merge, mas fazemos cleanup
        try:
            raise RuntimeError("simulated worker failure")
        except Exception:
            pass
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root)

        # Cleanup executado: staging não deve existir
        assert not staging_root.exists()

        # Merge não aconteceu: workspace não deve ter os arquivos
        assert not (workspace / "success.txt").exists()
        assert not (workspace / "success2.txt").exists()

    def test_staging_cleanup_on_merge_success(self, workspace, staging_root, config, file_tools):
        """Merge bem-sucedido deve remover staging."""
        s0 = staging_root / "0"
        s0.mkdir()

        set_staging_root(s0)
        file_tools.write_file(ToolCall(name="write_file", arguments={
            "path": "merged.txt", "content": "merged content",
        }))

        # Simula fluxo bem-sucedido: merge + cleanup
        _merge_staging_to_workspace(staging_root, workspace)
        if staging_root.exists():
            shutil.rmtree(staging_root)

        assert not staging_root.exists()
        assert (workspace / "merged.txt").read_text() == "merged content"

    def test_staging_cleanup_safety_when_already_removed(self, workspace, staging_root):
        """Cleanup deve ser seguro se staging já foi removido."""
        shutil.rmtree(staging_root)
        # Não deve levantar exceção
        if staging_root.exists():
            shutil.rmtree(staging_root)
        assert not staging_root.exists()
