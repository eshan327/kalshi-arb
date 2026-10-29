"""CF historical ticks and settlement state for baseline replay."""

from __future__ import annotations

import math
import time
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime
from typing import Any

from core.markets import MarketProfile, parse_iso8601_to_epoch
from data.kalshi_rest import get_cfbenchmarks_history

CF_HISTORY_MIN_INTERVAL_SEC = 0.26


def _parse_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return ts if math.isfinite(ts) and ts > 0 else None
    if isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return parse_iso8601_to_epoch(value)
        return _parse_timestamp(numeric)
    return None


def normalize_cf_history(rows: list[dict]) -> list[dict[str, float]]:
    """Preserve published milliseconds; never floor subsecond spot into a fixing."""
    values = {}
    for row in rows:
        ts = _parse_timestamp(row.get("time"))
        price = float(row["value"])
        if ts is None or not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid CF historical value")
        if ts in values and values[ts] != price:
            raise ValueError("Conflicting CF historical values")
        values[ts] = price
    return [{"ts": ts, "price": values[ts]} for ts in sorted(values)]


def one_second_boundary_ticks(
    ticks: list[dict[str, float]],
) -> list[dict[str, float]]:
    """Return only exact second-boundary CF values, matching cfbenchmarks_value."""
    return [tick for tick in ticks if float(tick["ts"]).is_integer()]


def _hour_start(ts: float) -> float:
    return float(int(ts) // 3600 * 3600)


def _iso_hour(ts: float) -> str:
    return (
        datetime.fromtimestamp(_hour_start(ts), UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def fetch_cf_range(
    index_id: str,
    start_ts: float,
    end_ts: float,
) -> list[dict[str, float]]:
    """Fetch a contiguous CF historical range at its published resolution."""
    ticks: list[dict[str, float]] = []
    cursor = _hour_start(start_ts)
    final_hour = _hour_start(end_ts)
    first = True
    while cursor <= final_hour:
        if not first:
            # Passthrough history reads cost 50 read tokens. Basic accounts have a
            # 200-token/sec budget, so pace bulk research at just under 4 req/sec.
            time.sleep(CF_HISTORY_MIN_INTERVAL_SEC)
        hourly = normalize_cf_history(
            get_cfbenchmarks_history(
                index_id,
                timestamp=_iso_hour(cursor),
                timespan="HOUR",
            )
        )
        for tick in hourly:
            if not start_ts <= tick["ts"] <= end_ts:
                continue
            if ticks and tick["ts"] <= ticks[-1]["ts"]:
                if tick == ticks[-1]:
                    continue  # Adjacent HOUR responses may share a boundary tick.
                raise ValueError("CF historical windows overlap out of order")
            ticks.append(tick)
        first = False
        cursor += 3600

    return ticks


def fetch_cf_feeds(
    profile: MarketProfile,
    start_ts: float,
    end_ts: float,
) -> tuple[list[dict[str, float]], list[dict[str, float]], str]:
    """
    Recreate the live 5 Hz/1 Hz split from CF's historical tick stream.

    Historical /history/values does not accept maxResolution. For subsecond RTIs it
    returns the published 200 ms ticks, including exact second-boundary values.
    The live bot uses all ticks only for current spot while the 1 Hz channel owns
    volatility history and settlement fixes, so replay filters those exact
    second-boundary publications explicitly.
    """
    published = fetch_cf_range(profile.index_id, start_ts, end_ts)
    if not published:
        raise ValueError("CF historical values are unavailable")

    fixes = one_second_boundary_ticks(published)
    if not fixes:
        raise ValueError("CF history contained no exact one-second boundary values")
    return published, fixes, "PER_200MS" if profile.high_frequency else "PER_SECOND"


def _ticks_between(
    ticks: list[dict[str, float]],
    start_ts: float,
    end_ts: float,
    *,
    start_inclusive: bool = True,
) -> list[dict[str, float]]:
    """Slice sorted ticks in O(log n + k) without rescanning the full history."""
    key = lambda row: row["ts"]
    left = (
        bisect_left(ticks, start_ts, key=key)
        if start_inclusive
        else bisect_right(ticks, start_ts, key=key)
    )
    right = bisect_right(ticks, end_ts, key=key)
    return ticks[left:right]


def _latest_tick(
    ticks: list[dict[str, float]], now_ts: float
) -> dict[str, float] | None:
    if not ticks:
        return None
    index = bisect_right(ticks, now_ts, key=lambda row: row["ts"])
    return None if index == 0 else ticks[index - 1]


def _settlement_state(
    fix_ticks: list[dict[str, float]],
    *,
    now_ts: float,
    close_ts: float,
    window: int,
    spot_ts: float | None = None,
) -> dict[str, Any]:
    latest_fix = _latest_tick(fix_ticks, now_ts)
    timestamp = (
        spot_ts
        if spot_ts is not None
        else (latest_fix["ts"] if latest_fix is not None else 0.0)
    )
    if latest_fix is None or timestamp <= 0:
        return {"connected": False, "timestamp": 0.0}

    state: dict[str, Any] = {
        "connected": True,
        "timestamp": float(timestamp),
    }
    start = close_ts - window
    if now_ts <= start:
        return state

    fixes = _ticks_between(
        fix_ticks,
        start,
        min(now_ts, close_ts),
        start_inclusive=False,
    )
    if not fixes:
        return state

    expected = list(range(int(start) + 1, math.floor(min(now_ts, close_ts)) + 1))
    if [tick["ts"] for tick in fixes] != expected:
        return state  # Missing fixes cannot be reconstructed from later spot values.

    mean = sum(tick["price"] for tick in fixes) / len(fixes)
    state.update(
        average_ts=latest_fix["ts"],
        final_average={
            "start": start,
            "end": fixes[-1]["ts"],
            "count": len(fixes),
            "value": mean,
        },
    )
    return state
