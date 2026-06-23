from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.patch import PatchTool


def test_apply_patch_updates_existing_file(tmp_path):
    """Verifica que apply_patch atualiza linhas existentes corretamente."""
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
    """Verifica que apply_patch cria novos arquivos via Add File."""
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
    """Verifica que apply_patch rejeita hunk que não encontra correspondência."""
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


def test_apply_patch_missing_hunk_reports_nearest_context(tmp_path):
    """Erro de hunk inclui trecho esperado e localização aproximada mais parecida."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "demo.txt").write_text(
        "alpha\nbeta atual\ngamma\n",
        encoding="utf-8",
    )

    tool = PatchTool(ToolRuntimeConfig(workspace_root=workspace))
    patch = "\n".join([
        "*** Begin Patch",
        "*** Update File: demo.txt",
        "@@",
        " alpha",
        "-beta antigo",
        "+beta novo",
        " gamma",
        "*** End Patch",
    ])

    result = tool.apply_patch(ToolCall(name="apply_patch", arguments={"patch": patch}))

    assert result.ok is False
    assert "Hunk não encontrado em demo.txt" in result.error
    assert "Trecho mais próximo começa na linha 1" in result.error
    assert "Esperado:" in result.error
    assert "beta antigo" in result.error
    assert "Encontrado próximo:" in result.error
    assert "beta atual" in result.error
