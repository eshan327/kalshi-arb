from __future__ import annotations

import time
from datetime import datetime


def parse_iso8601_to_epoch(value: str | None) -> float | None:
    if not isinstance(value, str) or not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def select_target_market(markets: list[dict]) -> dict:
    """Select the active market with the nearest future close timestamp."""
    now = time.time()
    candidates: list[tuple[float, dict]] = []

    for market in markets:
        close_ts = parse_iso8601_to_epoch(market.get("close_time"))
        if close_ts is None or close_ts > now:
            rank = close_ts if close_ts is not None else float("inf")
            candidates.append((rank, market))

    if not candidates:
        return markets[0]

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def is_market_closed(market_close_ts: float | None) -> bool:
    return market_close_ts is not None and time.time() >= market_close_ts
