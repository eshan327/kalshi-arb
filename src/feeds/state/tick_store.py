from __future__ import annotations

import time
from collections import deque
from threading import RLock
from typing import Any

from engine.settlement_sampling import compute_discrete_settlement_proxy

_current_brti: float | None = None
_current_depth: int = 0
_current_exchanges: int = 0
_current_brti_ts: float = 0.0
_current_asset: str = "BTC"
_tick_version: int = 0

_tick_lock = RLock()
_brti_ticks = deque(maxlen=2000)


def reset_tick_state(asset: str) -> None:
    global _current_brti, _current_depth, _current_exchanges, _current_brti_ts, _current_asset, _tick_version
    with _tick_lock:
        _current_brti = None
        _current_depth = 0
        _current_exchanges = 0
        _current_brti_ts = 0.0
        _current_asset = asset
        _brti_ticks.clear()
        _tick_version += 1


def record_brti_tick(
    brti: float | None,
    depth: int,
    num_exchanges: int,
    levels: dict[str, Any],
    status: str,
) -> None:
    global _tick_version
    with _tick_lock:
        _brti_ticks.append(
            {
                "ts": time.time(),
                "brti": brti,
                "depth": depth,
                "exchanges": num_exchanges,
                "levels": levels,
                "status": status,
            }
        )
        _tick_version += 1


def set_brti_state(brti: float, depth: int, exchanges: int, timestamp: float) -> None:
    global _current_brti, _current_depth, _current_exchanges, _current_brti_ts
    with _tick_lock:
        _current_brti = brti
        _current_depth = depth
        _current_exchanges = exchanges
        _current_brti_ts = timestamp


def get_brti_state() -> dict[str, float | int | None]:
    with _tick_lock:
        return {
            "brti": _current_brti,
            "depth": _current_depth,
            "exchanges": _current_exchanges,
            "timestamp": _current_brti_ts,
            "asset": _current_asset,
        }


def get_brti_ticks(limit: int = 200) -> list[dict[str, Any]]:
    with _tick_lock:
        if limit <= 0:
            return []
        return list(_brti_ticks)[-limit:]


def get_brti_tick_version() -> int:
    with _tick_lock:
        return _tick_version


def get_brti_settlement_proxy(window_seconds: int = 60) -> dict[str, float | int | None | str]:
    with _tick_lock:
        ticks_snapshot = list(_brti_ticks)

    return compute_discrete_settlement_proxy(
        ticks_snapshot,
        window_seconds=max(1, int(window_seconds)),
    )
