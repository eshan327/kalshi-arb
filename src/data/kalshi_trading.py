from __future__ import annotations

import time
import uuid
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any
from urllib.parse import urlparse

import requests

from core.auth import get_api_auth_headers
from core.config import API_BASE_URL

HTTP_TIMEOUT_SEC = 10.0
CLIENT_ORDER_PREFIX = "kalshi-algo-"

_api_call_lock = Lock()
_session = requests.Session()
_live_order_entry_enabled = False


class KalshiAPIError(RuntimeError):
    def __init__(self, status: int, detail):
        self.status = status
        super().__init__(f"Kalshi API {status}: {detail}")


def _pages(path: str, key: str, params: dict) -> list[dict]:
    rows, cursor = [], None
    seen = set()
    while True:
        payload = _request(
            "GET",
            path,
            params={**params, "limit": 1000, **({"cursor": cursor} if cursor else {})},
        )
        rows.extend(payload.get(key, []))
        cursor = payload.get("cursor")
        if not cursor:
            return rows
        if cursor in seen:
            raise RuntimeError("Repeated pagination cursor")
        seen.add(cursor)


def set_live_order_entry_enabled(enabled: bool) -> None:
    global _live_order_entry_enabled
    _live_order_entry_enabled = bool(enabled)


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    method = method.upper()
    sign_path = f"{urlparse(API_BASE_URL).path.rstrip('/')}{path}"
    headers = get_api_auth_headers(method, sign_path)
    if body is not None:
        headers["Content-Type"] = "application/json"

    with _api_call_lock:
        response = _session.request(
            method,
            f"{API_BASE_URL}{path}",
            params=params,
            json=body,
            headers=headers,
            timeout=HTTP_TIMEOUT_SEC,
        )

    if not response.ok:
        try:
            detail = response.json().get("error") or response.json()
        except ValueError:
            detail = response.text[:500]
        raise KalshiAPIError(response.status_code, detail)
    return response.json() if response.content else {}


def _client_order_id() -> str:
    return f"{CLIENT_ORDER_PREFIX}{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"


def place_limit_order(
    *,
    market_ticker: str,
    side: str,
    action: str,
    count: int | float | Decimal,
    price_cents: int | float | Decimal,
    client_order_id: str | None = None,
    allow_when_stopped: bool = False,
) -> dict[str, Any]:
    """Place a V2 IOC order using outcome-side semantics."""
    side = side.strip().lower()
    action = action.strip().lower()
    if side not in {"yes", "no"} or action not in {"buy", "sell"}:
        raise ValueError("side must be yes/no and action must be buy/sell")
    if not _live_order_entry_enabled and not (allow_when_stopped and action == "sell"):
        raise RuntimeError("Start live trading before submitting orders.")
    try:
        quantity = Decimal(str(count)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("count must be numeric") from exc
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("count must be positive")
    try:
        outcome_price = Decimal(str(price_cents)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("price_cents must be numeric") from exc
    if not outcome_price.is_finite() or not Decimal("0") < outcome_price < Decimal(
        "100"
    ):
        raise ValueError("price_cents must be between 0 and 100")

    # V2 quotes the YES book only: bid=buy YES/sell NO, ask=sell YES/buy NO.
    book_side = "bid" if (side == "yes") == (action == "buy") else "ask"
    yes_price_cents = outcome_price if side == "yes" else Decimal("100") - outcome_price
    order_id = client_order_id or _client_order_id()
    payload = _request(
        "POST",
        "/portfolio/events/orders",
        body={
            "ticker": market_ticker,
            "client_order_id": order_id,
            "side": book_side,
            "count": f"{quantity:.2f}",
            "price": f"{yes_price_cents / Decimal('100'):.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": True,
            "reduce_only": action == "sell",
        },
    )
    return {
        "ok": True,
        "client_order_id": order_id,
        "order": payload.get("order", payload),
    }


def get_positions(*, market_ticker: str | None = None) -> list[dict[str, Any]]:
    return _pages(
        "/portfolio/positions",
        "market_positions",
        {
            "count_filter": "position",
            **({"ticker": market_ticker} if market_ticker else {}),
        },
    )


def get_open_orders(*, market_ticker: str | None = None) -> list[dict[str, Any]]:
    return _pages(
        "/portfolio/orders",
        "orders",
        {"status": "resting", **({"ticker": market_ticker} if market_ticker else {})},
    )


def cancel_order(order_id: str, market_ticker: str) -> dict[str, Any]:
    return _request(
        "DELETE",
        f"/portfolio/events/orders/{order_id}",
        params={"market_ticker": market_ticker},
    )


def cancel_bot_orders(*, market_ticker: str | None = None) -> int:
    canceled = 0
    from data.account_state import bot_orders

    orders = bot_orders()
    if orders is None:
        orders = get_open_orders(market_ticker=market_ticker)
    for order in orders:
        if market_ticker and order.get("ticker") != market_ticker:
            continue
        client_id = str(order.get("client_order_id") or "")
        order_id = str(order.get("order_id") or "")
        if client_id.startswith(CLIENT_ORDER_PREFIX) and order_id:
            cancel_order(order_id, str(order["ticker"]))
            canceled += 1
    return canceled
