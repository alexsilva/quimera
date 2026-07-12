"""Legacy mock profile used by compatibility tests."""

from quimera.profiles.base import ExecutionProfile, register


register(ExecutionProfile(
    name="mock",
    prefix="/mock",
    icon="M",
    style=("white", "Mock"),
    cmd=["echo"],
    prompt_as_arg=True,
    capabilities=["test_execution"],
    preferred_task_types=["test_execution"],
    supports_tools=False,
    has_builtin_tools=False,
    supports_code_editing=False,
    supports_task_execution=True,
    supports_warm_pool=False,
    base_tier=1,
))
