from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.patch import PatchTool


def test_apply_patch_updates_existing_file(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "demo.txt"
    target.write_text("linha 1\nlinha 2\n", encoding="utf-8")

    tool = PatchTool(ToolRuntimeConfig(workspace_root=workspace))
    patch = "\n".join([
        "*** Begin Patch",
        "*** Update File: demo.txt",
        "@@",
        " linha 1",
        "-linha 2",
        "+linha 2 alterada",
        "*** End Patch",
    ])

    result = tool.apply_patch(ToolCall(name="apply_patch", arguments={"patch": patch}))

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "linha 1\nlinha 2 alterada\n"


def test_apply_patch_add_file(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    tool = PatchTool(ToolRuntimeConfig(workspace_root=workspace))
    patch = "\n".join([
        "*** Begin Patch",
        "*** Add File: novo.txt",
        "+conteudo",
        "*** End Patch",
    ])

    result = tool.apply_patch(ToolCall(name="apply_patch", arguments={"patch": patch}))

    assert result.ok is True
    assert (workspace / "novo.txt").read_text(encoding="utf-8") == "conteudo\n"


def test_apply_patch_rejects_missing_hunk(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "demo.txt").write_text("linha 1\nlinha 2\n", encoding="utf-8")

    tool = PatchTool(ToolRuntimeConfig(workspace_root=workspace))
    patch = "\n".join([
        "*** Begin Patch",
        "*** Update File: demo.txt",
        "@@",
        " linha 1",
        "-linha inexistente",
        "+linha nova",
        "*** End Patch",
    ])

    result = tool.apply_patch(ToolCall(name="apply_patch", arguments={"patch": patch}))

    assert result.ok is False
    assert "Hunk não encontrado" in result.error
