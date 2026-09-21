"""Private stream state. REST bootstraps/reconciles; it never runs per strategy tick."""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from threading import Lock, RLock

from data.kalshi_trading import (
    CLIENT_ORDER_PREFIX,
    _request,
    get_open_orders,
    get_positions,
)
from data.updates import notify

logger = logging.getLogger(__name__)
_lock = RLock()
_refresh_lock = Lock()
_enabled = False
_connected = False
_needs_snapshot = True
_revision = 0
_positions: dict[str, dict] = {}
_orders: dict[str, dict] = {}
_balance: dict = {}
_refreshed = 0.0
_dirty_since = 0.0
_pending: dict | None = None
_error: str | None = None


def connection(connected: bool) -> None:
    global _connected, _needs_snapshot, _revision
    with _lock:
        _connected = connected
        _needs_snapshot = True
        _revision += 1


def ingest(kind: str, msg: dict) -> None:
    global _revision, _dirty_since
    with _lock:
        if kind == "market_position":
            ticker = str(msg["market_ticker"])
            position = Decimal(msg["position_fp"])
            cost = Decimal(msg["position_cost_dollars"])
            if not position.is_finite() or not cost.is_finite():
                raise ValueError("Invalid streamed position")
            if position:
                _positions[ticker] = {
                    **msg,
                    "ticker": ticker,
                    "market_exposure_dollars": str(abs(cost)),
                }
            else:
                _positions.pop(ticker, None)
        elif kind == "user_order":
            client = str(msg.get("client_order_id", ""))
            if client.startswith(CLIENT_ORDER_PREFIX):
                if msg.get("status") == "resting" or (
                    _pending and client == _pending["client_order_id"]
                ):
                    _orders[client] = dict(msg)
                else:
                    _orders.pop(client, None)
        elif kind == "fill":
            if _pending and msg.get("client_order_id") == _pending["client_order_id"]:
                trade_id = str(msg["trade_id"])
                if trade_id not in _pending.setdefault("trade_ids", []):
                    _pending["trade_ids"].append(trade_id)
                    _pending["received_count"] = str(
                        Decimal(_pending.get("received_count", "0"))
                        + Decimal(msg["count_fp"])
                    )
                    _pending["post_position_fp"] = msg.get("post_position_fp")
        else:
            return
        _revision += 1
        _dirty_since = time.time()
    notify()


def begin_order(client_id: str, ticker: str) -> None:
    global _pending, _revision
    with _lock:
        if _pending is not None:
            raise RuntimeError(
                "Previous order is still reconciling; no duplicate submission."
            )
        if (
            not _connected
            or _needs_snapshot
            or time.time() - _refreshed > 15
            or _dirty_since
        ):
            raise RuntimeError("Live account is not synchronized.")
        _pending = dict(
            client_order_id=client_id, ticker=ticker, sent=time.time(), confirmed=False
        )
        _revision += 1


def order_response(order: dict) -> None:
    global _dirty_since, _revision
    with _lock:
        if _pending:
            _pending["confirmed"] = True
            _pending["fill_count"] = str(order["fill_count"])
            _dirty_since = time.time()
            _revision += 1


def order_rejected() -> None:
    global _pending, _dirty_since, _revision
    with _lock:
        _pending = None
        _dirty_since = time.time()
        _revision += 1


def _watermark() -> float:
    value = _request("GET", "/exchange/user_data_timestamp")["as_of_time"]
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def refresh() -> None:
    global \
        _balance, \
        _positions, \
        _orders, \
        _needs_snapshot, \
        _refreshed, \
        _dirty_since, \
        _pending, \
        _error
    if not _refresh_lock.acquire(blocking=False):
        return
    try:
        with _lock:
            if not _enabled or not _connected:
                return
            revision, bootstrap, dirty = _revision, _needs_snapshot, _dirty_since
            pending = deepcopy(_pending)
            streamed_position = (
                deepcopy(_positions.get(pending["ticker"], {})) if pending else {}
            )
            streamed_order = (
                deepcopy(_orders.get(pending["client_order_id"], {})) if pending else {}
            )
            if (
                not bootstrap
                and not dirty
                and not pending
                and time.time() - _refreshed < 5
            ):
                return
        # Kalshi documents REST replication lag. Do not let an old balance unlock
        # another order. This watermark is approximate; pending orders additionally
        # reconcile positions against REST once, after acknowledgement.
        if (dirty or pending) and _watermark() <= max(
            dirty, pending["sent"] if pending else 0
        ):
            return
        if pending and not pending["confirmed"]:
            # An HTTP timeout is ambiguous. Look up the same client ID; never resend.
            orders = (
                [streamed_order] if streamed_order else _request_orders_since(pending)
            )
            match = next(
                (
                    o
                    for o in orders
                    if o.get("client_order_id") == pending["client_order_id"]
                ),
                None,
            )
            if match is None or match.get("status") not in {"executed", "canceled"}:
                return
        # Position snapshots are only startup/reconnect or post-order reconciliation.
        # Streamed fills/positions otherwise update the cache without position polling.
        position_confirmed = not pending
        if pending and pending["confirmed"]:
            filled = Decimal(pending["fill_count"])
            position_confirmed = filled == 0 or (
                Decimal(pending.get("received_count", "-1")) == filled
                and pending.get("post_position_fp") is not None
                and Decimal(streamed_position.get("position_fp", "0"))
                == Decimal(pending["post_position_fp"])
            )
        positions = get_positions() if bootstrap or not position_confirmed else None
        orders = get_open_orders() if bootstrap else None
        balance = _request("GET", "/portfolio/balance")
        if not isinstance(balance.get("balance"), int) or not isinstance(
            balance.get("portfolio_value"), int
        ):
            raise ValueError("Invalid balance response")
        with _lock:
            if revision != _revision or not _connected:
                return
            if positions is not None:
                _positions = {
                    p["ticker"]: p for p in positions if Decimal(p["position_fp"]) != 0
                }
            if orders is not None:
                _orders = {
                    o["client_order_id"]: o
                    for o in orders
                    if str(o.get("client_order_id", "")).startswith(CLIENT_ORDER_PREFIX)
                }
            if pending:
                _orders.pop(pending["client_order_id"], None)
            _balance = balance
            _needs_snapshot = False
            _dirty_since = 0
            _pending = None
            _refreshed = time.time()
            _error = None
        notify()
    except Exception as exc:
        with _lock:
            _error = str(exc)
        logger.warning("Account reconciliation: %s", exc)
    finally:
        _refresh_lock.release()


def _request_orders_since(pending: dict) -> list[dict]:
    from data.kalshi_trading import _pages

    return _pages(
        "/portfolio/orders",
        "orders",
        {"ticker": pending["ticker"], "min_ts": int(pending["sent"]) - 1},
    )


def snapshot() -> dict:
    global _enabled
    with _lock:
        first = not _enabled
        _enabled = True
    if first:
        refresh()
    with _lock:
        if (
            not _connected
            or _needs_snapshot
            or not _balance
            or time.time() - _refreshed > 15
        ):
            raise RuntimeError(
                _error or "Live account syncing; wait for private streams and balance."
            )
        result = deepcopy(_balance)
        result.update(
            positions=deepcopy(list(_positions.values())),
            refreshed_ts=_refreshed,
            ready=not _dirty_since and _pending is None,
            pending_order=deepcopy(_pending),
        )
        return result


def bot_orders() -> list[dict] | None:
    with _lock:
        return (
            deepcopy(list(_orders.values()))
            if _connected and not _needs_snapshot
            else None
        )


async def run_account_sync() -> None:
    while True:
        await asyncio.to_thread(refresh)
        await asyncio.sleep(1)
