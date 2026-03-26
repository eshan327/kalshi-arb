import time
from collections import deque
from threading import Lock
from typing import Any
from core.config import ORDERBOOK_VIEW_DEPTH, WS_LOG_DEFAULT_LIMIT, WS_LOG_MAXLEN

_ws_message_log = deque(maxlen=WS_LOG_MAXLEN)
_top10_impact_log = deque(maxlen=WS_LOG_MAXLEN)
_reconciliation_log = deque(maxlen=WS_LOG_MAXLEN)
_ws_log_lock = Lock()
_ws_stats: dict[str, int] = {
    "total_received": 0,
    "orderbook_delta_received": 0,
    "orderbook_delta_buffered": 0,
    "orderbook_delta_applied": 0,
    "orderbook_delta_stale_ignored": 0,
    "orderbook_delta_invalid_seq": 0,
    "orderbook_delta_seq_gap": 0,
    "ticker_received": 0,
    "snapshot_anchor_seen": 0,
}


def _count_incoming_message(msg_type: str) -> None:
    with _ws_log_lock:
        _ws_stats["total_received"] += 1
        if msg_type == "orderbook_delta":
            _ws_stats["orderbook_delta_received"] += 1
        elif msg_type == "ticker":
            _ws_stats["ticker_received"] += 1


def _record_ws_event(event_type: str, seq: int | None, payload: dict[str, Any], status: str) -> None:
    entry = {
        "ts": time.time(),
        "type": event_type,
        "seq": seq,
        "status": status,
        "payload": payload,
    }
    with _ws_log_lock:
        _ws_message_log.append(entry)

        if event_type == "orderbook_delta":
            if status == "buffered":
                _ws_stats["orderbook_delta_buffered"] += 1
            elif status in {"applied", "applied_from_buffer"}:
                _ws_stats["orderbook_delta_applied"] += 1
            elif status in {"stale_ignored", "stale_buffer_ignored"}:
                _ws_stats["orderbook_delta_stale_ignored"] += 1
            elif status == "invalid_seq_ignored":
                _ws_stats["orderbook_delta_invalid_seq"] += 1
            elif status in {"seq_gap", "buffer_replay_gap"}:
                _ws_stats["orderbook_delta_seq_gap"] += 1

        if event_type == "orderbook_snapshot" and status == "anchor_seen":
            _ws_stats["snapshot_anchor_seen"] += 1


def _top10_signature(book) -> tuple:
    yes_bids, yes_asks, no_bids, no_asks = book.get_orderbook()

    def _norm(levels):
        return tuple((round(float(px), 2), float(qty)) for px, qty in levels[:ORDERBOOK_VIEW_DEPTH])

    return (
        _norm(yes_bids),
        _norm(yes_asks),
        _norm(no_bids),
        _norm(no_asks),
    )


def _record_top10_impact(seq: int, payload: dict[str, Any], changed: bool) -> None:
    with _ws_log_lock:
        _top10_impact_log.append(
            {
                "ts": time.time(),
                "seq": seq,
                "changed": changed,
                "payload": payload,
            }
        )


def _record_reconciliation(event: dict[str, Any]) -> None:
    with _ws_log_lock:
        _reconciliation_log.append(event)


def get_ws_message_log(limit: int = 200) -> list[dict[str, Any]]:
    """Returns newest websocket events for debugging and validation."""
    with _ws_log_lock:
        if limit <= 0:
            return []
        return list(_ws_message_log)[-limit:]


def get_top10_impact_log(limit: int = WS_LOG_DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Returns newest top-10 impact records for orderbook-focused verification."""
    with _ws_log_lock:
        if limit <= 0:
            return []
        return list(_top10_impact_log)[-limit:]


def get_ws_message_log_size() -> int:
    """Returns current number of retained websocket log entries."""
    with _ws_log_lock:
        return len(_ws_message_log)


def get_reconciliation_log(limit: int = WS_LOG_DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Returns newest reconciliation checks and resync decisions."""
    with _ws_log_lock:
        if limit <= 0:
            return []
        return list(_reconciliation_log)[-limit:]


def get_ws_processing_stats() -> dict[str, int]:
    """Returns aggregate counters proving websocket events are processed end-to-end."""
    with _ws_log_lock:
        return dict(_ws_stats)
