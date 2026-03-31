from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="opencode-pickle",
    prefix="/opencode-pickle",
    cmd=["opencode", "--model=opencode/big-pickle", "run"],
    style=("blue", "OpenCodePickle"),
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-gpt",
    prefix="/opencode-gpt",
    cmd=["opencode", "--model=opencode/gpt-5-nano", "run"],
    style=("blue", "OpenCodeGPT"),
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-mimo-omni",
    prefix="/opencode-mimo-omni",
    cmd=["opencode", "--model=opencode/mimo-v2-omni-free", "run"],
    style=("blue", "OpenCodeMimoOmni"),
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-omni-pro",
    prefix="/opencode-omni-pro",
    cmd=["opencode", "--model=opencode/mimo-v2-pro-free", "run"],
    style=("blue", "OpenCodeOmniPro"),
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-minimax",
    prefix="/opencode-minimax",
    cmd=["opencode", "--model=opencode/minimax-m2.5-free", "run"],
    style=("blue", "OpenCodeMiniMax"),
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-nemotron",
    prefix="/opencode-nemotron",
    cmd=["opencode", "--model=opencode/nemotron-3-super-free", "run"],
    style=("blue", "OpenCodeNemotron"),
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-qwen",
    prefix="/opencode-qwen",
    cmd=["opencode", "--model=opencode/qwen3.6-plus-free", "run"],
    style=("blue", "OpenCodeQwen"),
)
register(plugin)