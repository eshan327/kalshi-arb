import time
from collections import deque
from threading import RLock
from typing import Any, TypedDict


class ExchangeBook(TypedDict):
    bids: dict[float, float]
    asks: dict[float, float]
    last_update: float

# Per-exchange L2 orderbook state
# {exchange: {"bids": {price: size}, "asks": {price: size}, "last_update": float}}
exchange_books: dict[str, ExchangeBook] = {}

# Latest BRTI output
current_brti: float | None = None
current_depth: int = 0
current_exchanges: int = 0
current_brti_ts: float = 0.0

_brti_ticks = deque(maxlen=2000)
_exchange_ws_log = deque(maxlen=5000)
_state_lock = RLock()
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


def safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def init_exchange_book(exchange: str) -> None:
    """Initializes an empty orderbook for an exchange on (re)connect."""
    with _state_lock:
        exchange_books[exchange] = {
            "bids": {},
            "asks": {},
            "last_update": 0,
        }


def mark_book_update_applied(count: int = 1) -> None:
    with _state_lock:
        _exchange_ws_stats["book_updates_applied"] += max(1, int(count))


def update_level(exchange: str, side: str, price: float, size: float) -> None:
    """Applies an incremental L2 update on one side of one exchange book."""
    with _state_lock:
        if exchange not in exchange_books:
            init_exchange_book(exchange)

        book = exchange_books[exchange][side]
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        exchange_books[exchange]["last_update"] = time.time()


def replace_full_book(exchange: str, bids: dict[float, float], asks: dict[float, float]) -> None:
    """Atomically swaps full snapshot books to avoid exposing partial state."""
    with _state_lock:
        if exchange not in exchange_books:
            init_exchange_book(exchange)
        exchange_books[exchange]["bids"] = bids
        exchange_books[exchange]["asks"] = asks
        exchange_books[exchange]["last_update"] = time.time()
    mark_book_update_applied(len(bids) + len(asks))


def record_exchange_ws_message(exchange: str, raw_data: dict[str, Any], status: str) -> None:
    with _state_lock:
        suffix = "received" if status == "received" else "parsed"
        total_key = "total_received" if suffix == "received" else "total_parsed"
        _exchange_ws_stats[total_key] += 1

        key = f"{exchange.lower()}_{suffix}"
        if key in _exchange_ws_stats:
            _exchange_ws_stats[key] += 1

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


def record_brti_tick(
    brti: float | None,
    depth: int,
    num_exchanges: int,
    levels: dict[str, Any],
    status: str,
) -> None:
    with _state_lock:
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


def set_brti_state(brti: float, depth: int, exchanges: int, timestamp: float) -> None:
    global current_brti, current_depth, current_exchanges, current_brti_ts
    with _state_lock:
        current_brti = brti
        current_depth = depth
        current_exchanges = exchanges
        current_brti_ts = timestamp


def get_exchange_books_ref() -> dict[str, ExchangeBook]:
    """Returns live exchange books reference for in-loop BRTI math computation."""
    return exchange_books


def get_brti_state() -> dict[str, float | int | None]:
    """Returns latest BRTI snapshot for downstream consumers."""
    with _state_lock:
        return {
            "brti": current_brti,
            "depth": current_depth,
            "exchanges": current_exchanges,
            "timestamp": current_brti_ts,
        }


def get_brti_ticks(limit: int = 200) -> list[dict[str, Any]]:
    """Returns newest synthesized BRTI ticks for dashboard verification."""
    with _state_lock:
        if limit <= 0:
            return []
        return list(_brti_ticks)[-limit:]


def get_brti_settlement_proxy(window_seconds: int = 60) -> dict[str, float | int | None]:
    """Returns rolling average of valid BRTI prints over the given lookback window."""
    now = time.time()
    cutoff = now - max(1, window_seconds)

    with _state_lock:
        window_values = [
            float(tick["brti"])
            for tick in _brti_ticks
            if tick.get("status") == "ok"
            and isinstance(tick.get("brti"), (int, float))
            and tick.get("ts", 0) >= cutoff
        ]

    if not window_values:
        return {
            "window_seconds": window_seconds,
            "samples": 0,
            "average": None,
        }

    return {
        "window_seconds": window_seconds,
        "samples": len(window_values),
        "average": round(sum(window_values) / len(window_values), 2),
    }


def get_brti_ws_log(limit: int = 200) -> list[dict[str, Any]]:
    """Returns newest raw exchange websocket events feeding BRTI."""
    with _state_lock:
        if limit <= 0:
            return []
        return list(_exchange_ws_log)[-limit:]


def get_brti_ws_stats() -> dict[str, int]:
    """Returns aggregate counters proving BRTI websocket message processing."""
    with _state_lock:
        return dict(_exchange_ws_stats)
