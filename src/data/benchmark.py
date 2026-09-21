"""Official CF values and server-calculated averages; no synthetic constituents."""

from __future__ import annotations

import json
import math
import time
from collections import deque
from copy import deepcopy
from threading import RLock

_lock = RLock()
_ticks: deque[dict] = deque(maxlen=2000)  # Only the 1 Hz channel enters history.
_state: dict = {}
_version = 0


def reset_tick_state(asset: str) -> None:
    global _version
    with _lock:
        _ticks.clear()
        _state.clear()
        _state.update(asset=asset, price=None, timestamp=0.0, connected=False)
        _version += 1


def set_index_connected(connected: bool) -> None:
    global _version
    with _lock:
        _state["connected"] = connected
        _version += 1


def _average(raw: dict | None) -> dict | None:
    if raw is None:
        return None
    value = float(raw["value"])
    count = int(raw["window_size"])
    start = int(raw["window_start_ts_ms"]) / 1000
    end = int(raw["window_end_ts_exclusive"]) / 1000
    if not math.isfinite(value) or value <= 0 or not 0 <= count <= 60 or end < start:
        raise ValueError("Invalid CF average")
    return dict(value=value, count=count, start=start, end=end)


def ingest_index(channel: str, msg: dict, index_id: str) -> bool:
    """Use vendor timestamps; 5 Hz updates spot, 1 Hz also updates history/averages."""
    global _version
    if msg.get("index_id") != index_id:
        return False
    fast = channel == "cfbenchmarks_value_5hz"
    raw = {} if fast else json.loads(msg["data"])
    price = float(msg["value_usd"] if fast else raw["value"])
    ts = float(msg["source_ts_ms"] if fast else raw["time"]) / 1000
    if (
        not math.isfinite(price)
        or price <= 0
        or not math.isfinite(ts)
        or ts <= 0
        or ts > time.time() + 2
    ):
        raise ValueError("Invalid CF tick")
    trailing = None if fast else _average(msg.get("avg_60s_data"))
    final = None if fast else _average(msg.get("last_60s_windowed_average_15min"))
    with _lock:
        if ts <= _state.get(channel, 0):
            return False
        _state[channel] = ts
        if ts >= _state.get("timestamp", 0):
            _state.update(price=price, timestamp=ts)
        if not fast:
            _ticks.append(
                dict(
                    ts=ts, price=price, average=trailing["value"] if trailing else None
                )
            )
            _state.update(trailing_average=trailing, final_average=final, average_ts=ts)
        _version += 1
    return True


def get_index_state() -> dict:
    with _lock:
        return deepcopy(_state)


def get_index_ticks(limit: int = 2000) -> list[dict]:
    with _lock:
        return [dict(tick) for tick in list(_ticks)[-limit:]] if limit > 0 else []


def get_index_tick_version() -> int:
    with _lock:
        return _version


def seed_history(payload: list[dict]) -> None:
    """Merge recent official 1 Hz history without changing live spot or averages."""
    global _version
    now = time.time()
    history = {}
    for tick in payload:
        ts, price = float(tick["time"]) / 1000, float(tick["value"])
        if not math.isfinite(ts) or not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid historical benchmark")
        if now - 2000 <= ts <= now and ts.is_integer():
            history[ts] = dict(ts=ts, price=price, average=None)
    with _lock:
        history.update({tick["ts"]: tick for tick in _ticks})
        _ticks.clear()
        _ticks.extend(history[ts] for ts in sorted(history)[-2000:])
        _version += 1
