from __future__ import annotations

from typing import Any

from feeds.state.book_store import (
    ExchangeBook,
    get_exchange_books_ref,
    init_exchange_book,
    replace_full_book,
    safe_float,
    update_level,
)
from feeds.state.diagnostics_store import (
    get_brti_ws_log as _get_brti_ws_log,
    get_brti_ws_stats as _get_brti_ws_stats,
    mark_book_update_applied,
    record_exchange_ws_message,
)
from feeds.state.runtime_state import reset_brti_runtime_state
from feeds.state.tick_store import (
    get_brti_settlement_proxy as _get_brti_settlement_proxy,
    get_brti_state as _get_brti_state,
    get_brti_tick_version as _get_brti_tick_version,
    get_brti_ticks as _get_brti_ticks,
    record_brti_tick,
    set_brti_state,
)

__all__ = [
    "ExchangeBook",
    "safe_float",
    "init_exchange_book",
    "mark_book_update_applied",
    "update_level",
    "replace_full_book",
    "record_exchange_ws_message",
    "record_brti_tick",
    "set_brti_state",
    "get_exchange_books_ref",
    "get_brti_state",
    "get_brti_ticks",
    "get_brti_settlement_proxy",
    "get_brti_tick_version",
    "get_brti_ws_log",
    "get_brti_ws_stats",
    "reset_brti_runtime_state",
]


# Backward-compatible type hints for callers expecting concrete return annotation shapes.
def get_brti_settlement_proxy(window_seconds: int = 60) -> dict[str, float | int | None | str]:
    return _get_brti_settlement_proxy(window_seconds=window_seconds)


def get_brti_state() -> dict[str, float | int | None]:
    return _get_brti_state()


def get_brti_ticks(limit: int = 200) -> list[dict[str, Any]]:
    return _get_brti_ticks(limit=limit)


def get_brti_tick_version() -> int:
    return _get_brti_tick_version()


def get_brti_ws_log(limit: int = 200) -> list[dict[str, Any]]:
    return _get_brti_ws_log(limit=limit)


def get_brti_ws_stats() -> dict[str, int]:
    return _get_brti_ws_stats()
