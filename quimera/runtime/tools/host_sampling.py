"""Cálculos puros para amostragem de processos do host.

Mantém análise de séries separada do acesso a ``/proc`` e da superfície de tools.
Nenhuma função deste módulo classifica crescimento como leak; ela apenas resume
medidas observadas ao longo de uma janela.
"""
from __future__ import annotations

from itertools import pairwise
from typing import Any

_SAMPLE_METRICS = (
    "rss_kb",
    "hwm_kb",
    "vm_size_kb",
    "vm_data_kb",
    "swap_kb",
    "threads",
    "children",
    "fds",
    "sockets",
    "pipes",
    "inotify_fds",
    "inotify_watches",
    "cpu_time_seconds",
    "cpu_percent",
)

_GROWTH_METRICS = (
    "rss_kb",
    "vm_data_kb",
    "swap_kb",
    "threads",
    "fds",
    "sockets",
    "inotify_watches",
)


def cpu_percent(previous: dict[str, Any], current: dict[str, Any]) -> float | None:
    """Calcula uso de um núcleo entre duas amostras de CPU time."""
    elapsed = float(current["elapsed_seconds"]) - float(previous["elapsed_seconds"])
    if elapsed <= 0:
        return None
    cpu_delta = float(current["cpu_time_seconds"]) - float(previous["cpu_time_seconds"])
    if cpu_delta < 0:
        return None
    return round((cpu_delta / elapsed) * 100.0, 3)


def summarize_samples(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Resume todas as métricas conhecidas em uma série de amostras."""
    summary: dict[str, dict[str, Any]] = {}
    for metric in _SAMPLE_METRICS:
        metric_summary = summarize_series(samples, metric)
        if metric_summary is not None:
            summary[metric] = metric_summary
    return summary


def summarize_series(
    samples: list[dict[str, Any]], metric: str
) -> dict[str, Any] | None:
    """Calcula extremos, delta e regressão linear de uma métrica."""
    points = [
        (float(sample["elapsed_seconds"]), float(value))
        for sample in samples
        if (value := sample.get(metric)) is not None
    ]
    if not points:
        return None

    first = points[0][1]
    last = points[-1][1]
    delta = last - first
    elapsed = points[-1][0] - points[0][0]
    rate = delta / elapsed if elapsed > 0 else 0.0
    mean = sum(value for _time, value in points) / len(points)
    slope = linear_slope(points)
    positive_steps = sum(
        1 for previous, current in pairwise(points) if current[1] > previous[1]
    )
    negative_steps = sum(
        1 for previous, current in pairwise(points) if current[1] < previous[1]
    )
    if slope > 1e-9:
        direction = "up"
    elif slope < -1e-9:
        direction = "down"
    else:
        direction = "flat"
    return {
        "first": compact_number(first),
        "last": compact_number(last),
        "min": compact_number(min(value for _time, value in points)),
        "max": compact_number(max(value for _time, value in points)),
        "mean": compact_number(mean),
        "delta": compact_number(delta),
        "rate_per_second": round(rate, 6),
        "slope_per_second": round(slope, 6),
        "direction": direction,
        "positive_steps": positive_steps,
        "negative_steps": negative_steps,
        "points": len(points),
    }


def linear_slope(points: list[tuple[float, float]]) -> float:
    """Retorna o coeficiente angular da regressão linear simples."""
    if len(points) < 2:
        return 0.0
    mean_x = sum(x for x, _y in points) / len(points)
    mean_y = sum(y for _x, y in points) / len(points)
    denominator = sum((x - mean_x) ** 2 for x, _y in points)
    if denominator <= 0:
        return 0.0
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return numerator / denominator


def compact_number(value: float) -> int | float:
    """Preserva inteiros no payload sem perder precisão útil de floats."""
    rounded = round(value, 6)
    if float(rounded).is_integer():
        return int(rounded)
    return rounded


def growth_signals(summary: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Lista métricas cujo valor final ficou acima do inicial.

    O nome deliberadamente fala em crescimento observado, não leak. Incluímos a
    direção da regressão e contagem de passos para que uma subida líquida com
    oscilação não seja confundida com crescimento monotônico.
    """
    signals: list[dict[str, Any]] = []
    for metric in _GROWTH_METRICS:
        row = summary.get(metric)
        if row is None or float(row.get("delta") or 0) <= 0:
            continue
        signals.append(
            {
                "metric": metric,
                "delta": row["delta"],
                "slope_per_second": row["slope_per_second"],
                "direction": row["direction"],
                "positive_steps": row["positive_steps"],
                "negative_steps": row["negative_steps"],
            }
        )
    return signals


def format_sample_summary(payload: dict[str, Any]) -> str:
    """Formata a visão compacta destinada ao modelo/terminal."""
    summary = payload["summary"]
    state_flags = [
        key for key in ("ended", "cancelled", "pid_reused") if payload.get(key)
    ]
    state = ",".join(state_flags) if state_flags else "running"
    lines = [
        f"pid={payload['pid']} name={payload['name']} state={state}",
        (
            f"samples={payload['sample_count']}/{payload['target_sample_count']} "
            f"duration={payload['actual_duration_seconds']}s interval={payload['interval_ms']}ms"
        ),
    ]
    for metric in ("rss_kb", "vm_data_kb", "threads", "fds", "inotify_watches"):
        row = summary.get(metric)
        if row is None:
            continue
        lines.append(
            f"{metric} first={row['first']} last={row['last']} delta={row['delta']} "
            f"slope={row['slope_per_second']}/s min={row['min']} max={row['max']}"
        )
    cpu = summary.get("cpu_percent")
    if cpu is not None:
        lines.append(
            f"cpu_percent mean={cpu['mean']} max={cpu['max']} last={cpu['last']}"
        )
    if payload["growth_observed"]:
        lines.append(f"growth_observed={payload['growth_observed']}")
    return "\n".join(lines)
