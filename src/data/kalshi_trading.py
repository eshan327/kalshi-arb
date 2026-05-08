from __future__ import annotations

import time
import uuid
from threading import Lock
from typing import Any

from core.auth import get_authenticated_client
from core.config import EXECUTION_MODE, EXECUTION_ORDER_TIME_IN_FORCE

_client_lock = Lock()
_cached_client = None
_api_call_lock = Lock()


def _client():
    global _cached_client
    with _client_lock:
        if _cached_client is None:
            _cached_client = get_authenticated_client()
        return _cached_client


def _to_dict(model_or_dict: Any) -> dict[str, Any]:
    if model_or_dict is None:
        return {}
    if isinstance(model_or_dict, dict):
        return dict(model_or_dict)
    if hasattr(model_or_dict, "to_dict"):
        return dict(model_or_dict.to_dict())
    return {}


def _normalize_order(order: Any) -> dict[str, Any]:
    payload = _to_dict(order)
    if payload.get("order_id") is None and payload.get("id") is not None:
        payload["order_id"] = payload.get("id")
    return payload


def _normalize_fill(fill: Any) -> dict[str, Any]:
    return _to_dict(fill)


def _build_client_order_id(prefix: str = "arb") -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"


def _ensure_live_mode() -> None:
    if EXECUTION_MODE != "live":
        raise RuntimeError(
            "Kalshi trading adapter is disabled unless KALSHI_EXECUTION_MODE=live."
        )


def place_limit_order(
    *,
    market_ticker: str,
    side: str,
    action: str = "buy",
    count: int,
    price_cents: int,
    post_only: bool,
    time_in_force: str | None = None,
    client_order_id: str | None = None,
    expiration_ts: int | None = None,
) -> dict[str, Any]:
    _ensure_live_mode()
    c = _client()

    side_norm = str(side).strip().lower()
    if side_norm not in {"yes", "no"}:
        raise ValueError(f"Invalid side '{side}'.")

    action_norm = str(action).strip().lower()
    if action_norm not in {"buy", "sell"}:
        raise ValueError(f"Invalid action '{action}'.")

    px = max(1, min(99, int(price_cents)))
    comp = 100 - px
    order_id = client_order_id or _build_client_order_id(prefix="live")

    req: dict[str, Any] = {
        "ticker": str(market_ticker),
        "client_order_id": order_id,
        "side": side_norm,
        "action": action_norm,
        "count": max(1, int(count)),
        "type": "limit",
        "time_in_force": time_in_force or EXECUTION_ORDER_TIME_IN_FORCE,
        "post_only": bool(post_only),
        "cancel_order_on_pause": True,
    }

    if side_norm == "yes":
        req["yes_price"] = px
        req["no_price"] = comp
    else:
        req["no_price"] = px
        req["yes_price"] = comp

    if isinstance(expiration_ts, int) and expiration_ts > 0:
        req["expiration_ts"] = expiration_ts

    with _api_call_lock:
        response = c.create_order(**req)
    response_payload = _to_dict(response)
    order_payload = _normalize_order(response_payload.get("order"))

    return {
        "ok": bool(order_payload),
        "order": order_payload,
        "raw": response_payload,
        "client_order_id": order_id,
    }


def cancel_order(order_id: str) -> dict[str, Any]:
    _ensure_live_mode()
    c = _client()
    with _api_call_lock:
        response = c.cancel_order(order_id=str(order_id))
    payload = _to_dict(response)
    return {
        "ok": True,
        "order": _normalize_order(payload.get("order")),
        "reduced_by": payload.get("reduced_by"),
        "raw": payload,
    }


def get_open_orders(*, market_ticker: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    _ensure_live_mode()
    c = _client()
    cursor: str | None = None
    remaining = max(1, min(200, int(limit)))
    out: list[dict[str, Any]] = []

    while remaining > 0:
        with _api_call_lock:
            page = c.get_orders(
                ticker=market_ticker,
                status="resting",
                limit=min(200, remaining),
                cursor=cursor,
            )
        payload = _to_dict(page)
        orders = payload.get("orders")
        if isinstance(orders, list):
            for item in orders:
                out.append(_normalize_order(item))

        cursor = payload.get("cursor") if isinstance(payload.get("cursor"), str) else None
        if not cursor or not orders:
            break
        remaining = max(0, int(limit) - len(out))

    return out


def get_positions(*, market_ticker: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    _ensure_live_mode()
    c = _client()
    cursor: str | None = None
    remaining = max(1, min(1000, int(limit)))
    out: list[dict[str, Any]] = []

    while remaining > 0:
        with _api_call_lock:
            page = c.get_positions(
                cursor=cursor,
                limit=min(1000, remaining),
                count_filter="position,total_traded",
                ticker=market_ticker,
            )
        payload = _to_dict(page)
        positions = payload.get("market_positions")
        if isinstance(positions, list):
            for item in positions:
                if isinstance(item, dict):
                    out.append(dict(item))
                elif hasattr(item, "to_dict"):
                    out.append(dict(item.to_dict()))

        cursor = payload.get("cursor") if isinstance(payload.get("cursor"), str) else None
        if not cursor or not positions:
            break
        remaining = max(0, int(limit) - len(out))

    return out


def get_recent_fills(
    *,
    market_ticker: str | None = None,
    min_ts: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    _ensure_live_mode()
    c = _client()
    cursor: str | None = None
    remaining = max(1, min(200, int(limit)))
    out: list[dict[str, Any]] = []

    while remaining > 0:
        with _api_call_lock:
            page = c.get_fills(
                ticker=market_ticker,
                min_ts=min_ts,
                limit=min(200, remaining),
                cursor=cursor,
            )
        payload = _to_dict(page)
        fills = payload.get("fills")
        if isinstance(fills, list):
            for item in fills:
                out.append(_normalize_fill(item))

        cursor = payload.get("cursor") if isinstance(payload.get("cursor"), str) else None
        if not cursor or not fills:
            break
        remaining = max(0, int(limit) - len(out))

    return out


def get_balance_summary() -> dict[str, int]:
    _ensure_live_mode()
    c = _client()
    with _api_call_lock:
        payload = _to_dict(c.get_balance())
    return {
        "balance": int(payload.get("balance", 0) or 0),
        "portfolio_value": int(payload.get("portfolio_value", 0) or 0),
        "updated_ts": int(payload.get("updated_ts", 0) or 0),
    }
