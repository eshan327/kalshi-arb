"""Explicit REST order calls for Predictions V2 and Perps; no automatic trading."""

from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import requests

from core.auth import get_api_auth_headers
from core.config import API_BASE_URL, PERPS_API_BASE_URL
from data import kalshi_rest


class OrderOutcomeUnknown(RuntimeError):
    """A write may have reached Kalshi. Reconcile before submitting another write."""


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a nonempty string without outer whitespace")
    return value


def _integer(value, name: str, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"Invalid {name}")
    return value


def _decimal(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Use a decimal string, Decimal, or integer; floats are not accepted")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal value") from exc
    if not number.is_finite():
        raise ValueError("Decimal value must be finite")
    return number


def _fixed(value, places: int, *, allow_zero: bool = False) -> str:
    number = _decimal(value)
    if number < 0 or (number == 0 and not allow_zero):
        raise ValueError("Value must be positive (or zero for reduce_to)")
    try:
        rounded = number.quantize(Decimal(1).scaleb(-places))
    except InvalidOperation as exc:
        raise ValueError("Decimal value is too large") from exc
    if number != rounded:
        raise ValueError(f"Value must have at most {places} decimal places; no rounding is performed")
    return f"{rounded:.{places}f}"


class OrderClient:
    """Single-order lifecycle for an explicitly selected product.

    Constructing this client and building payloads perform no I/O. Create/amend
    require current market metadata and caller-generated client IDs. Recorders
    and backtests never instantiate this client.
    """

    def __init__(self, product: str):
        if product not in {"predictions", "perps"}:
            raise ValueError("product must be predictions or perps")
        self.product = product
        self.base_url = (API_BASE_URL if product == "predictions" else PERPS_API_BASE_URL).rstrip("/")
        self.write_path = "/portfolio/events/orders" if product == "predictions" else "/orders"
        self.read_path = "/portfolio/orders" if product == "predictions" else "/orders"

    def _routing(self, *, subaccount=None, exchange_index=None) -> dict:
        fields = {}
        if subaccount is not None:
            fields["subaccount"] = _integer(subaccount, "subaccount", 0, 63)
        if exchange_index is not None:
            if self.product == "perps":
                raise ValueError("Perps order endpoints do not accept exchange_index")
            fields["exchange_index"] = _integer(exchange_index, "exchange_index", -1)
        return fields

    def _terms(self, market: dict, side: str, count, price) -> dict:
        ticker = _text(market.get("ticker"), "market ticker")
        if side not in {"bid", "ask"}:
            raise ValueError("side must be bid or ask; Predictions prices always use the Yes scale")
        count_str, price_str = _fixed(count, 2), _fixed(price, 4)
        quantity, quote = Decimal(count_str), Decimal(price_str)
        if self.product == "predictions":
            if not 0 < quote < 1:
                raise ValueError("Predictions price must be between 0 and 1 dollars")
            ranges = market.get("price_ranges")
            if not isinstance(ranges, list) or not ranges:
                raise ValueError("Current Predictions price_ranges are required")
            on_grid = False
            for band in ranges:
                start, end, step = (_decimal(band[key]) for key in ("start", "end", "step"))
                if not 0 <= start < end <= 1 or step <= 0:
                    raise ValueError("Invalid market price range")
                on_grid |= start <= quote <= end and (quote - start) % step == 0
            if not on_grid:
                raise ValueError("Price is outside this market's price_ranges grid")
        else:
            tick = _decimal(market.get("tick_size"))
            if tick <= 0 or quote % tick:
                raise ValueError("Price must match the Perps market tick_size")
            fractional = market.get("fractional_trading_enabled")
            if type(fractional) is not bool:
                raise ValueError("Perps fractional_trading_enabled metadata is required")
            if not fractional and quantity != quantity.to_integral_value():
                raise ValueError("This Perps market requires whole contracts")
        return {"ticker": ticker, "side": side, "count": count_str, "price": price_str}

    def build_order(
        self, market: dict, *, side: str, count, price, client_order_id: str,
        time_in_force: str, self_trade_prevention_type: str = "taker_at_cross",
        post_only: bool = False, reduce_only: bool = False,
        cancel_order_on_pause: bool = True, expiration_time: int | None = None,
        subaccount: int | None = None, exchange_index: int | None = None,
        order_group_id: str | None = None,
    ) -> dict:
        """Build a validated payload without networking or silently snapping prices."""
        body = self._terms(market, side, count, price)
        _text(client_order_id, "client_order_id")
        if time_in_force not in {"good_till_canceled", "immediate_or_cancel", "fill_or_kill"}:
            raise ValueError("Invalid time_in_force")
        if self_trade_prevention_type not in {"taker_at_cross", "maker"}:
            raise ValueError("Invalid self_trade_prevention_type")
        for flag in (post_only, reduce_only, cancel_order_on_pause):
            if type(flag) is not bool:
                raise ValueError("Order flags must be booleans")
        allowed_reduce_tif = {"immediate_or_cancel"}
        if self.product == "perps":
            allowed_reduce_tif.add("fill_or_kill")
        if reduce_only and time_in_force not in allowed_reduce_tif:
            raise ValueError("reduce_only requires IOC (or FOK on Perps)")
        if expiration_time is not None:
            _integer(expiration_time, "expiration_time", int(time.time()) + 1)
            if time_in_force != "good_till_canceled":
                raise ValueError("expiration_time requires good_till_canceled")
            body["expiration_time"] = expiration_time
        if order_group_id is not None:
            body["order_group_id"] = _text(order_group_id, "order_group_id")
        if self.product == "predictions" and exchange_index is None:
            exchange_index = market.get("exchange_index")
        body.update(self._routing(subaccount=subaccount, exchange_index=exchange_index))
        body.update(
            client_order_id=client_order_id, time_in_force=time_in_force,
            self_trade_prevention_type=self_trade_prevention_type, post_only=post_only,
            reduce_only=reduce_only, cancel_order_on_pause=cancel_order_on_pause,
        )
        return body

    def _order_path(self, order_id: str, *, read: bool = False) -> str:
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", order_id):
            raise ValueError("Invalid order_id")
        return f"{self.read_path if read else self.write_path}/{order_id}"

    def _write(self, method: str, path: str, *, body=None, params=None) -> dict:
        url = f"{self.base_url}{path}"
        identity = (body or {}).get("updated_client_order_id") or (body or {}).get("client_order_id") or path
        unknown = f"{method} {path}: outcome unknown for {identity}; reconcile orders/fills before retrying"
        # ponytail: one attempt, no write retry loop; add measured pacing when trading is automated.
        try:
            response = kalshi_rest._public_session.request(
                method, url, json=body, params=params,
                headers=get_api_auth_headers(method, urlparse(url).path),
                timeout=kalshi_rest.HTTP_TIMEOUT_SEC, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise OrderOutcomeUnknown(unknown) from exc
        if response.status_code >= 500:
            raise OrderOutcomeUnknown(unknown) from requests.HTTPError(response=response)
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise OrderOutcomeUnknown(unknown)
        try:
            payload = response.json()
        except ValueError as exc:
            raise OrderOutcomeUnknown(unknown) from exc
        if not isinstance(payload, dict) or not payload.get("order_id"):
            raise OrderOutcomeUnknown(unknown)
        return payload

    def create_order(self, market: dict, **fields) -> dict:
        return self._write("POST", self.write_path, body=self.build_order(market, **fields))

    def get_order(self, order_id: str) -> dict:
        return kalshi_rest._get_json(
            f"{self.base_url}{self._order_path(order_id, read=True)}", authenticated=True,
        )

    def get_orders(self, **filters) -> dict:
        """One page of current orders; follow cursor for reconciliation (not client-ID filtering)."""
        allowed = {"ticker", "status", "limit", "cursor", "min_ts", "max_ts", "subaccount"}
        if self.product == "predictions":
            allowed |= {"event_ticker", "exchange_index"}
        if filters.keys() - allowed:
            raise ValueError(f"Unsupported order filters: {filters.keys() - allowed}")
        filters = {key: value for key, value in filters.items() if value is not None}
        self._routing(subaccount=filters.get("subaccount"), exchange_index=filters.get("exchange_index"))
        return kalshi_rest._get_json(
            f"{self.base_url}{self.read_path}", authenticated=True, params=filters,
        )

    def amend_order(
        self, order_id: str, market: dict, *, side: str, count, price,
        client_order_id: str | None = None, updated_client_order_id: str | None = None,
        subaccount: int | None = None, exchange_index: int | None = None,
    ) -> dict:
        """count is already-filled quantity plus desired remaining quantity, not remaining alone."""
        body = self._terms(market, side, count, price)
        routing = self._routing(subaccount=subaccount, exchange_index=exchange_index)
        if self.product == "predictions":
            shard = exchange_index if exchange_index is not None else market.get("exchange_index")
            body.update(self._routing(exchange_index=shard))
        for name, value in (("client_order_id", client_order_id), ("updated_client_order_id", updated_client_order_id)):
            if value is not None:
                body[name] = _text(value, name)
        return self._write(
            "POST", f"{self._order_path(order_id)}/amend", body=body,
            params={key: value for key, value in routing.items() if key == "subaccount"},
        )

    def _cancel_routing(self, *, market_ticker=None, subaccount=None, exchange_index=None) -> dict:
        routing = self._routing(subaccount=subaccount, exchange_index=exchange_index)
        if self.product == "predictions":
            if market_ticker is not None:
                routing["market_ticker"] = _text(market_ticker, "market_ticker")
            elif exchange_index is None or exchange_index == -1:
                raise ValueError("Predictions cancel/decrease requires market_ticker or an explicit shard")
        elif market_ticker is not None:
            raise ValueError("Perps cancel/decrease routes by order_id; market_ticker is not a parameter")
        return routing

    def cancel_order(self, order_id: str, *, market_ticker=None, subaccount=None, exchange_index=None) -> dict:
        routing = self._cancel_routing(
            market_ticker=market_ticker, subaccount=subaccount, exchange_index=exchange_index,
        )
        return self._write("DELETE", self._order_path(order_id), params=routing)

    def decrease_order(
        self, order_id: str, *, reduce_by=None, reduce_to=None,
        market_ticker=None, subaccount=None, exchange_index=None,
    ) -> dict:
        if (reduce_by is None) == (reduce_to is None):
            raise ValueError("Provide exactly one of reduce_by or reduce_to")
        routing = self._cancel_routing(
            market_ticker=market_ticker, subaccount=subaccount, exchange_index=exchange_index,
        )
        body = {key: value for key, value in routing.items() if key != "subaccount"}
        field = "reduce_by" if reduce_by is not None else "reduce_to"
        body[field] = _fixed(reduce_by if reduce_by is not None else reduce_to, 2, allow_zero=field == "reduce_to")
        return self._write(
            "POST", f"{self._order_path(order_id)}/decrease", body=body,
            params={key: value for key, value in routing.items() if key == "subaccount"},
        )
