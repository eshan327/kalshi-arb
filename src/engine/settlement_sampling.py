from __future__ import annotations

import time
from typing import Any


def extract_valid_index_points(ticks: list[dict[str, Any]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for tick in ticks:
        if not isinstance(tick, dict) or tick.get("status") != "ok":
            continue

        ts = tick.get("ts")
        value = tick.get("brti")
        if not isinstance(ts, (int, float)):
            continue
        if not isinstance(value, (int, float)) or float(value) <= 0:
            continue

        points.append((float(ts), float(value)))

    if len(points) > 1:
        points.sort(key=lambda item: item[0])

    return points


def reconstruct_discrete_forward_fill_samples(
    points: list[tuple[float, float]],
    window_start_ts: float,
    window_end_ts: float,
    *,
    max_staleness_sec: float,
) -> tuple[list[float], int]:
    elapsed_seconds = int(max(0.0, float(window_end_ts) - float(window_start_ts)))
    if elapsed_seconds <= 0:
        return [], 0
    if not points:
        return [], elapsed_seconds

    idx = 0
    point_count = len(points)
    last_value: float | None = None
    last_value_ts: float | None = None

    while idx < point_count and points[idx][0] <= window_start_ts:
        last_value = points[idx][1]
        last_value_ts = points[idx][0]
        idx += 1

    samples: list[float] = []
    for second in range(1, elapsed_seconds + 1):
        target_ts = window_start_ts + second
        while idx < point_count and points[idx][0] <= target_ts:
            last_value = points[idx][1]
            last_value_ts = points[idx][0]
            idx += 1

        if last_value is None or last_value_ts is None:
            continue
        if (target_ts - last_value_ts) > max_staleness_sec:
            continue

        samples.append(last_value)

    return samples, elapsed_seconds


def compute_discrete_settlement_proxy(
    ticks: list[dict[str, Any]],
    *,
    window_seconds: int,
    max_staleness_sec: float = 5.0,
    now_ts: float | None = None,
) -> dict[str, float | int | None | str]:
    window = max(1, int(window_seconds))
    now = float(now_ts) if isinstance(now_ts, (int, float)) else time.time()
    cutoff = now - window

    points = extract_valid_index_points(ticks)
    samples, elapsed_seconds = reconstruct_discrete_forward_fill_samples(
        points,
        cutoff,
        now,
        max_staleness_sec=max_staleness_sec,
    )

    if not samples:
        return {
            "window_seconds": window,
            "samples": 0,
            "elapsed_seconds": elapsed_seconds,
            "method": "discrete_1s_forward_fill",
            "average": None,
        }

    return {
        "window_seconds": window,
        "samples": len(samples),
        "elapsed_seconds": elapsed_seconds,
        "method": "discrete_1s_forward_fill",
        "average": round(sum(samples) / len(samples), 2),
    }
