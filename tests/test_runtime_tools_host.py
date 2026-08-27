from __future__ import annotations

import os
from pathlib import Path

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicyError
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
    utime_ticks: int = 0,
    stime_ticks: int = 0,
    start_ticks: int = 1000,
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
    stat_tail = [
        "S",
        str(ppid),
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        str(utime_ticks),
        str(stime_ticks),
        "0",
        "0",
        "20",
        "0",
        str(threads),
        "0",
        str(start_ticks),
    ]
    (root / "stat").write_text(
        f"{pid} ({name}) " + " ".join(stat_tail) + "\n",
        encoding="utf-8",
    )
    children = root / "task" / str(pid)
    children.mkdir(parents=True)
    (children / "children").write_text("", encoding="utf-8")
    (root / "fd").mkdir()
    (root / "fdinfo").mkdir()
    return root


def _update_process_metrics(
    root: Path,
    *,
    rss_kb: int,
    hwm_kb: int,
    threads: int,
    utime_ticks: int,
    stime_ticks: int = 0,
) -> None:
    lines = []
    for line in (root / "status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            line = f"VmRSS:\t{rss_kb} kB"
        elif line.startswith("VmHWM:"):
            line = f"VmHWM:\t{hwm_kb} kB"
        elif line.startswith("Threads:"):
            line = f"Threads:\t{threads}"
        lines.append(line)
    (root / "status").write_text("\n".join(lines) + "\n", encoding="utf-8")

    pid = int(root.name)
    name = (root / "comm").read_text(encoding="utf-8").strip()
    stat_tail = [
        "S",
        "1",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        str(utime_ticks),
        str(stime_ticks),
        "0",
        "0",
        "20",
        "0",
        str(threads),
        "0",
        "1000",
    ]
    (root / "stat").write_text(
        f"{pid} ({name}) " + " ".join(stat_tail) + "\n",
        encoding="utf-8",
    )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


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


def test_host_process_sample_reports_deltas_slopes_cpu_and_inotify(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    root = _write_process(
        proc_root,
        44,
        uid=1000,
        rss_kb=100,
        hwm_kb=100,
        threads=2,
        utime_ticks=0,
    )
    os.symlink("anon_inode:inotify", root / "fd" / "7")
    os.symlink("socket:[123]", root / "fd" / "8")

    clock = _FakeClock()
    tools = HostTools(
        ToolRuntimeConfig(workspace_root=tmp_path),
        proc_root=proc_root,
        owner_uid=1000,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        clock_ticks_per_second=100,
    )
    progress: list[str] = []
    tools._set_progress_callback(progress.append)
    original_reader = tools._read_process_sample

    def dynamic_reader(*args, elapsed_seconds: float, **kwargs):
        if elapsed_seconds < 0.25:
            rss, hwm, threads, ticks, watches = 100, 100, 2, 0, 1
        elif elapsed_seconds < 0.75:
            rss, hwm, threads, ticks, watches = 200, 200, 3, 25, 2
        else:
            rss, hwm, threads, ticks, watches = 350, 350, 4, 60, 3
        _update_process_metrics(
            root,
            rss_kb=rss,
            hwm_kb=hwm,
            threads=threads,
            utime_ticks=ticks,
        )
        (root / "fdinfo" / "7").write_text(
            "pos:\t0\n"
            + "".join(
                f"inotify wd:{index} ino:{index} sdev:1 mask:fff ignored_mask:0\n"
                for index in range(1, watches + 1)
            ),
            encoding="utf-8",
        )
        return original_reader(*args, elapsed_seconds=elapsed_seconds, **kwargs)

    tools._read_process_sample = dynamic_reader
    result = tools.host_process_sample(
        ToolCall(
            "host_process_sample",
            {"pid": 44, "duration_seconds": 1.0, "interval_ms": 500},
        )
    )

    assert result.ok is True
    assert result.data["sample_count"] == 3
    assert result.data["actual_duration_seconds"] == 1.0
    assert result.data["summary"]["rss_kb"]["delta"] == 250
    assert result.data["summary"]["rss_kb"]["slope_per_second"] == 250.0
    assert result.data["summary"]["threads"]["delta"] == 2
    assert result.data["summary"]["inotify_watches"]["delta"] == 2
    assert result.data["summary"]["cpu_percent"]["mean"] == 60
    assert result.data["summary"]["cpu_percent"]["max"] == 70
    assert {item["metric"] for item in result.data["growth_observed"]} >= {
        "rss_kb",
        "threads",
        "inotify_watches",
    }
    assert len(progress) == 3
    assert "rss_kb first=100 last=350 delta=250" in result.content


def test_host_process_sample_cancels_cooperatively_between_samples(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(proc_root, 45, uid=1000)
    clock = _FakeClock()
    tools = HostTools(
        ToolRuntimeConfig(workspace_root=tmp_path),
        proc_root=proc_root,
        owner_uid=1000,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        clock_ticks_per_second=100,
    )
    tools._set_cancel_checker(lambda: clock.now >= 0.3)

    result = tools.host_process_sample(
        ToolCall(
            "host_process_sample",
            {"pid": 45, "duration_seconds": 2.0, "interval_ms": 500},
        )
    )

    assert result.ok is True
    assert result.data["cancelled"] is True
    assert result.data["sample_count"] == 1
    assert result.data["target_sample_count"] == 5


def test_host_process_sample_includes_final_partial_interval(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(proc_root, 47, uid=1000)
    clock = _FakeClock()
    tools = HostTools(
        ToolRuntimeConfig(workspace_root=tmp_path),
        proc_root=proc_root,
        owner_uid=1000,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    result = tools.host_process_sample(
        ToolCall(
            "host_process_sample",
            {"pid": 47, "duration_seconds": 1.0, "interval_ms": 600},
        )
    )

    assert result.ok is True
    assert result.data["target_sample_count"] == 3
    assert result.data["sample_count"] == 3
    assert result.data["actual_duration_seconds"] == 1.0
    assert [sample["elapsed_seconds"] for sample in result.data["samples"]] == [
        0.0,
        0.6,
        1.0,
    ]


def test_host_process_sample_detects_pid_reuse(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    root = _write_process(proc_root, 46, uid=1000, start_ticks=1000)
    clock = _FakeClock()
    tools = HostTools(
        ToolRuntimeConfig(workspace_root=tmp_path),
        proc_root=proc_root,
        owner_uid=1000,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )
    original_sleep = clock.sleep

    def reuse_pid_after_first_interval(seconds: float) -> None:
        original_sleep(seconds)
        if clock.now >= 0.5:
            text = (root / "stat").read_text(encoding="utf-8")
            fields = text.rstrip().rsplit(" ", 1)
            (root / "stat").write_text(fields[0] + " 2000\n", encoding="utf-8")

    tools._sleep = reuse_pid_after_first_interval
    result = tools.host_process_sample(
        ToolCall(
            "host_process_sample",
            {"pid": 46, "duration_seconds": 1.0, "interval_ms": 500},
        )
    )

    assert result.ok is True
    assert result.data["pid_reused"] is True
    assert result.data["sample_count"] == 1


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
    validator.validate(
        ToolCall(
            "host_process_sample",
            {"pid": 1, "duration_seconds": 5, "interval_ms": 500},
        )
    )
    validator.validate(ToolCall("host_memory", {}))

    with pytest.raises(ToolPolicyError):
        validator.validate(
            ToolCall(
                "host_process_sample",
                {"pid": 1, "duration_seconds": 60, "interval_ms": 100},
            )
        )

    with pytest.raises(ToolPolicyError):
        validator.validate(
            ToolCall(
                "host_process_sample",
                {"pid": 1, "duration_seconds": 1, "interval_ms": 2000},
            )
        )
