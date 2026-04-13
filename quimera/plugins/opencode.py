"""Componentes de `quimera.plugins.opencode`."""
from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="opencode-pickle",
    prefix="/opencode-pickle",
    cmd=["opencode", "--model=opencode/big-pickle", "run"],
    style=("blue", "OpenCodePickle"),
    capabilities=["general_coding", "code_review", "code_editing"],
    preferred_task_types=["code_edit", "code_review"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=True,
    supports_long_context=False, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-gpt",
    prefix="/opencode-gpt",
    cmd=["opencode", "--model=opencode/gpt-5-nano", "run"],
    style=("blue", "OpenCodeGPT"),
    capabilities=["general_reasoning", "code_review", "documentation"],
    preferred_task_types=["documentation", "code_review"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=False,
    supports_long_context=False, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-mimo-omni",
    prefix="/opencode-mimo-omni",
    cmd=["opencode", "--model=opencode/mimo-v2-omni-free", "run"],
    style=("blue", "OpenCodeMimoOmni"),
    capabilities=["general_reasoning", "code_review"],
    preferred_task_types=["code_review"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=True,
    supports_long_context=False, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-omni-pro",
    prefix="/opencode-omni-pro",
    cmd=["opencode", "--model=opencode/mimo-v2-pro-free", "run"],
    style=("blue", "OpenCodeOmniPro"),
    capabilities=["architecture", "code_review", "planning"],
    preferred_task_types=["architecture", "code_review"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=True,
    supports_long_context=True, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-minimax",
    prefix="/opencode-minimax",
    cmd=["opencode", "--model=opencode/minimax-m2.5-free", "run"],
    style=("blue", "OpenCodeMiniMax"),
    capabilities=["documentation", "code_review", "general_reasoning"],
    preferred_task_types=["documentation", "code_review"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=True,
    supports_long_context=False, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-nemotron",
    prefix="/opencode-nemotron",
    cmd=["opencode", "--model=opencode/nemotron-3-super-free", "run"],
    style=("blue", "OpenCodeNemotron"),
    capabilities=["bug_investigation", "code_review", "general_reasoning"],
    preferred_task_types=["bug_investigation", "code_review"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=True,
    supports_long_context=False, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-qwen",
    prefix="/opencode-qwen",
    cmd=["opencode", "--model=opencode/qwen3.6-plus-free", "run"],
    style=("blue", "OpenCodeQwen"),
    capabilities=["code_editing", "code_review", "bug_investigation", "general_coding"],
    preferred_task_types=["code_edit", "code_review", "bug_investigation"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=True,
    supports_long_context=False, base_tier=2,
)
register(plugin)
