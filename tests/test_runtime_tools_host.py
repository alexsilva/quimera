from __future__ import annotations

import os
from pathlib import Path

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.host import HostTools, HostToolsValidator


def _write_process(
    proc_root: Path,
    pid: int,
    *,
    uid: int,
    name: str = "python",
    ppid: int = 1,
    rss_kb: int = 100,
    hwm_kb: int = 200,
    threads: int = 4,
    cmdline: list[str] | None = None,
) -> Path:
    root = proc_root / str(pid)
    root.mkdir(parents=True)
    (root / "status").write_text(
        "\n".join(
            [
                f"Name:\t{name}",
                "State:\tS (sleeping)",
                f"PPid:\t{ppid}",
                f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}",
                f"VmRSS:\t{rss_kb} kB",
                f"VmHWM:\t{hwm_kb} kB",
                "VmSize:\t1000 kB",
                "VmData:\t500 kB",
                "VmSwap:\t5 kB",
                f"Threads:\t{threads}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    argv = cmdline or ["python", "app.py"]
    (root / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")
    (root / "comm").write_text(name + "\n", encoding="utf-8")
    children = root / "task" / str(pid)
    children.mkdir(parents=True)
    (children / "children").write_text("", encoding="utf-8")
    (root / "fd").mkdir()
    (root / "fdinfo").mkdir()
    return root


def test_host_processes_filters_same_user_sorts_and_redacts_secrets(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root,
        10,
        uid=1000,
        rss_kb=50,
        cmdline=["node", "server.js", "--token", "super-secret"],
    )
    _write_process(proc_root, 11, uid=1000, rss_kb=500, name="vite")
    _write_process(proc_root, 12, uid=2000, rss_kb=900, name="foreign")

    tools = HostTools(ToolRuntimeConfig(workspace_root=tmp_path), proc_root=proc_root, owner_uid=1000)
    result = tools.host_processes(ToolCall("host_processes", {"sort": "rss"}))

    assert result.ok is True
    assert [item["pid"] for item in result.data["processes"]] == [11, 10]
    assert "super-secret" not in result.content
    assert "<redacted>" in result.content
    assert result.data["same_user_only"] is True


def test_host_process_inspect_counts_inotify_watches_and_fds(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    root = _write_process(proc_root, 22, uid=1000)

    os.symlink("anon_inode:inotify", root / "fd" / "7")
    (root / "fdinfo" / "7").write_text(
        "pos:\t0\n"
        "inotify wd:1 ino:1 sdev:1 mask:fff ignored_mask:0\n"
        "inotify wd:2 ino:2 sdev:1 mask:fff ignored_mask:0\n",
        encoding="utf-8",
    )
    os.symlink("socket:[123]", root / "fd" / "8")
    os.symlink("pipe:[456]", root / "fd" / "9")

    tools = HostTools(ToolRuntimeConfig(workspace_root=tmp_path), proc_root=proc_root, owner_uid=1000)
    result = tools.host_process_inspect(ToolCall("host_process_inspect", {"pid": 22}))

    assert result.ok is True
    assert result.data["fds"]["total"] == 3
    assert result.data["fds"]["inotify_fds"] == 1
    assert result.data["fds"]["inotify_watches"] == 2
    assert result.data["fds"]["sockets"] == 1
    assert result.data["fds"]["pipes"] == 1


def test_host_process_inspect_rejects_other_users(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(proc_root, 33, uid=2000)

    tools = HostTools(ToolRuntimeConfig(workspace_root=tmp_path), proc_root=proc_root, owner_uid=1000)
    result = tools.host_process_inspect(ToolCall("host_process_inspect", {"pid": 33}))

    assert result.ok is False
    assert "não pertence ao usuário atual" in str(result.error)


def test_host_memory_parses_meminfo_load_and_pressure(tmp_path: Path):
    proc_root = tmp_path / "proc"
    (proc_root / "pressure").mkdir(parents=True)
    (proc_root / "meminfo").write_text(
        "MemTotal: 1000 kB\nMemAvailable: 400 kB\nMemFree: 100 kB\n"
        "Cached: 200 kB\nBuffers: 50 kB\nSwapTotal: 300 kB\nSwapFree: 250 kB\n",
        encoding="utf-8",
    )
    (proc_root / "loadavg").write_text("0.10 0.20 0.30 1/100 999\n", encoding="utf-8")
    (proc_root / "uptime").write_text("123.5 10.0\n", encoding="utf-8")
    (proc_root / "pressure" / "memory").write_text(
        "some avg10=0.10 avg60=0.20 avg300=0.30 total=42\n"
        "full avg10=0.01 avg60=0.02 avg300=0.03 total=4\n",
        encoding="utf-8",
    )

    tools = HostTools(ToolRuntimeConfig(workspace_root=tmp_path), proc_root=proc_root, owner_uid=1000)
    result = tools.host_memory(ToolCall("host_memory", {}))

    assert result.ok is True
    assert result.data["mem_used_kb"] == 600
    assert result.data["swap_used_kb"] == 50
    assert result.data["pressure"]["some"]["total"] == 42


def test_host_validator_rejects_invalid_arguments(tmp_path: Path):
    validator = HostToolsValidator(ToolRuntimeConfig(workspace_root=tmp_path))

    validator.validate(ToolCall("host_processes", {"limit": 10, "sort": "rss"}))
    validator.validate(ToolCall("host_process_inspect", {"pid": 1}))
    validator.validate(ToolCall("host_memory", {}))
