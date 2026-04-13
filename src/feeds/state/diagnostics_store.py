from __future__ import annotations

import time
from collections import deque
from threading import RLock
from typing import Any

_log_lock = RLock()
_exchange_ws_log = deque(maxlen=5000)
_exchange_ws_stats: dict[str, int] = {
    "total_received": 0,
    "total_parsed": 0,
    "coinbase_received": 0,
    "coinbase_parsed": 0,
    "kraken_received": 0,
    "kraken_parsed": 0,
    "gemini_received": 0,
    "gemini_parsed": 0,
    "bitstamp_received": 0,
    "bitstamp_parsed": 0,
    "paxos_received": 0,
    "paxos_parsed": 0,
    "book_updates_applied": 0,
}


def _zeroed_ws_stats() -> dict[str, int]:
    return {
        "total_received": 0,
        "total_parsed": 0,
        "coinbase_received": 0,
        "coinbase_parsed": 0,
        "kraken_received": 0,
        "kraken_parsed": 0,
        "gemini_received": 0,
        "gemini_parsed": 0,
        "bitstamp_received": 0,
        "bitstamp_parsed": 0,
        "paxos_received": 0,
        "paxos_parsed": 0,
        "book_updates_applied": 0,
    }


def reset_diagnostics_state() -> None:
    with _log_lock:
        _exchange_ws_log.clear()
        _exchange_ws_stats.clear()
        _exchange_ws_stats.update(_zeroed_ws_stats())


def mark_book_update_applied(count: int = 1) -> None:
    with _log_lock:
        _exchange_ws_stats["book_updates_applied"] += max(1, int(count))


def record_exchange_ws_message(exchange: str, raw_data: dict[str, Any], status: str) -> None:
    with _log_lock:
        suffix = "received" if status == "received" else "parsed"
        total_key = "total_received" if suffix == "received" else "total_parsed"
        _exchange_ws_stats[total_key] += 1

        per_exchange_key = f"{exchange.lower()}_{suffix}"
        if per_exchange_key in _exchange_ws_stats:
            _exchange_ws_stats[per_exchange_key] += 1

        _exchange_ws_log.append(
            {
                "ts": time.time(),
                "exchange": exchange,
                "status": status,
                "raw_type": raw_data.get("type") if isinstance(raw_data, dict) else None,
                "raw_channel": raw_data.get("channel") if isinstance(raw_data, dict) else None,
                "raw_event": raw_data.get("event") if isinstance(raw_data, dict) else None,
            }
        )


def get_brti_ws_log(limit: int = 200) -> list[dict[str, Any]]:
    with _log_lock:
        if limit <= 0:
            return []
        return list(_exchange_ws_log)[-limit:]


def get_brti_ws_stats() -> dict[str, int]:
    with _log_lock:
        return dict(_exchange_ws_stats)
