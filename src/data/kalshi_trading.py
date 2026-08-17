from __future__ import annotations

import time
import uuid
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any
from urllib.parse import urlparse

import requests

from core.auth import get_api_auth_headers
from core.config import API_BASE_URL, LIVE_TRADING_ENABLED

HTTP_TIMEOUT_SEC = 10.0
CLIENT_ORDER_PREFIX = "kalshi-algo-"

_api_call_lock = Lock()
_session = requests.Session()


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
        raise RuntimeError(f"Kalshi API {response.status_code}: {detail}")
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
) -> dict[str, Any]:
    """Place a V2 IOC order using outcome-side semantics at the strategy boundary."""
    if not LIVE_TRADING_ENABLED:
        raise RuntimeError("Live order entry requires KALSHI_LIVE_TRADING_ENABLED=true.")

    side = side.strip().lower()
    action = action.strip().lower()
    if side not in {"yes", "no"} or action not in {"buy", "sell"}:
        raise ValueError("side must be yes/no and action must be buy/sell")
    try:
        quantity = Decimal(str(count)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("count must be numeric") from exc
    if quantity <= 0:
        raise ValueError("count must be positive")
    try:
        outcome_price = Decimal(str(price_cents)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("price_cents must be numeric") from exc
    if not Decimal("0") < outcome_price < Decimal("100"):
        raise ValueError("price_cents must be between 0 and 100")

    # V2 quotes the YES book only: bid=buy YES/sell NO, ask=sell YES/buy NO.
    book_side = "bid" if (side == "yes") == (action == "buy") else "ask"
    yes_price_cents = (
        outcome_price if side == "yes" else Decimal("100") - outcome_price
    )
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


def get_balance_summary() -> dict[str, int]:
    payload = _request("GET", "/portfolio/balance")
    return {
        "balance": int(payload.get("balance") or 0),
        "portfolio_value": int(payload.get("portfolio_value") or 0),
        "updated_ts": int(payload.get("updated_ts") or 0),
    }


def get_positions(*, market_ticker: str | None = None) -> list[dict[str, Any]]:
    payload = _request(
        "GET",
        "/portfolio/positions",
        params={
            "limit": 100,
            "count_filter": "position",
            **({"ticker": market_ticker} if market_ticker else {}),
        },
    )
    positions = payload.get("market_positions")
    return [dict(item) for item in positions] if isinstance(positions, list) else []


def get_open_orders(*, market_ticker: str | None = None) -> list[dict[str, Any]]:
    payload = _request(
        "GET",
        "/portfolio/orders",
        params={
            "status": "resting",
            "limit": 100,
            **({"ticker": market_ticker} if market_ticker else {}),
        },
    )
    orders = payload.get("orders")
    return [dict(item) for item in orders] if isinstance(orders, list) else []


def cancel_order(order_id: str) -> dict[str, Any]:
    return _request("DELETE", f"/portfolio/events/orders/{order_id}")


def cancel_bot_orders(*, market_ticker: str | None = None) -> int:
    canceled = 0
    for order in get_open_orders(market_ticker=market_ticker):
        client_id = str(order.get("client_order_id") or "")
        order_id = str(order.get("order_id") or "")
        if client_id.startswith(CLIENT_ORDER_PREFIX) and order_id:
            cancel_order(order_id)
            canceled += 1
    return canceled
