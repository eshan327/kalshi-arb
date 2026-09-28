import json
from decimal import Decimal

import pytest
import requests

from data import kalshi_orders as orders

PREDICTION = {
    "ticker": "KXBTC15M-EXAMPLE", "exchange_index": 2,
    "price_ranges": [
        {"start": "0", "end": "0.1", "step": "0.001"},
        {"start": "0.1", "end": "0.9", "step": "0.01"},
        {"start": "0.9", "end": "1", "step": "0.001"},
    ],
}
PERP = {"ticker": "KXBTCPERP", "tick_size": "0.000100", "fractional_trading_enabled": True}
FIELDS = {"side": "bid", "count": "2.50", "price": "0.42", "client_order_id": "intent-1", "time_in_force": "immediate_or_cancel"}


def response(status=201, payload=None):
    result = requests.Response()
    result.status_code = status
    result.url = "https://example.com/orders"
    result._content = json.dumps(payload if payload is not None else {"order_id": "order-1", "fill_count": "0.00", "remaining_count": "2.50"}).encode()
    return result


def capture_requests(monkeypatch, result=None):
    calls, signatures = [], []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return result if result is not None else response()

    def sign(method, path):
        signatures.append((method, path))
        return {"KALSHI-ACCESS-SIGNATURE": "signed"}

    monkeypatch.setattr(orders.kalshi_rest._public_session, "request", request)
    monkeypatch.setattr(orders, "get_api_auth_headers", sign)
    monkeypatch.setattr(orders, "API_BASE_URL", "https://example.com/trade-api/v2")
    monkeypatch.setattr(orders, "PERPS_API_BASE_URL", "https://perps.example.com/trade-api/v2/margin")
    return calls, signatures


def test_building_is_offline_and_prediction_ask_keeps_yes_price(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Building an order must not load keys or send requests")

    monkeypatch.setattr(orders, "get_api_auth_headers", forbidden)
    monkeypatch.setattr(orders.kalshi_rest._public_session, "request", forbidden)
    client = orders.OrderClient("predictions")
    payload = client.build_order(PREDICTION, **{**FIELDS, "side": "ask", "price": "0.58"})
    assert payload == {
        "ticker": "KXBTC15M-EXAMPLE", "side": "ask", "price": "0.5800", "count": "2.50",
        "client_order_id": "intent-1", "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross", "post_only": False,
        "reduce_only": False, "cancel_order_on_pause": True, "exchange_index": 2,
    }
    assert "subaccount" not in payload  # Restricted keys may omit their locked subaccount.
    assert client.build_order(PREDICTION, **{**FIELDS, "price": Decimal("0.091")})["price"] == "0.0910"


@pytest.mark.parametrize("product,market,path", [
    ("predictions", PREDICTION, "https://example.com/trade-api/v2/portfolio/events/orders"),
    ("perps", PERP, "https://perps.example.com/trade-api/v2/margin/orders"),
])
def test_create_signed_current_routes_once(monkeypatch, product, market, path):
    calls, signatures = capture_requests(monkeypatch)
    client = orders.OrderClient(product)
    fields = {**FIELDS, "price": "65000.1234"} if product == "perps" else FIELDS
    assert client.create_order(market, **fields)["order_id"] == "order-1"
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert (method, url) == ("POST", path)
    assert signatures == [("POST", "/" + path.split("/", 3)[3])]
    assert kwargs["json"]["client_order_id"] == "intent-1"
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == 10
    assert "action" not in kwargs["json"] and "yes_price" not in kwargs["json"]
    assert ("exchange_index" in kwargs["json"]) == (product == "predictions")


@pytest.mark.parametrize("field,value", [
    ("count", "0"), ("count", "1.001"), ("price", "NaN"),
    ("price", "0.421"), ("subaccount", True),
])
def test_invalid_order_rejected_before_io(monkeypatch, field, value):
    calls, _ = capture_requests(monkeypatch)
    with pytest.raises(ValueError):
        orders.OrderClient("predictions").create_order(PREDICTION, **{**FIELDS, field: value})
    assert calls == []


@pytest.mark.parametrize("product,market,prefix", [
    ("predictions", PREDICTION, "/trade-api/v2/portfolio/events/orders"),
    ("perps", PERP, "/trade-api/v2/margin/orders"),
])
def test_amend_decrease_cancel_routing_and_zero_remaining(monkeypatch, product, market, prefix):
    calls, signatures = capture_requests(monkeypatch)
    client = orders.OrderClient(product)
    client.amend_order("order-1", market, side="bid", count="3.50", price="0.43", subaccount=4)
    assert calls[-1][2]["json"]["count"] == "3.50"  # Total/max fillable, passed unchanged.
    assert calls[-1][2]["params"] == {"subaccount": 4}
    assert "subaccount" not in calls[-1][2]["json"]
    routing = {"market_ticker": market["ticker"], "exchange_index": 2} if product == "predictions" else {}
    client.decrease_order("order-1", reduce_to=0, subaccount=4, **routing)
    assert calls[-1][2]["json"] == {"reduce_to": "0.00", **routing}
    assert calls[-1][2]["params"] == {"subaccount": 4}
    client.cancel_order("order-1", subaccount=4, **routing)
    assert calls[-1][2]["params"] == {"subaccount": 4, **routing}
    assert signatures == [("POST", prefix + "/order-1/amend"), ("POST", prefix + "/order-1/decrease"), ("DELETE", prefix + "/order-1")]
    with pytest.raises(ValueError, match="exactly one"):
        client.decrease_order("order-1", reduce_by=1, reduce_to=0, **routing)
    if product == "predictions":
        with pytest.raises(ValueError, match="market_ticker"):
            client.cancel_order("order-1")
        client.cancel_order("order-1", market_ticker=market["ticker"])
        assert calls[-1][2]["params"] == {"market_ticker": market["ticker"]}
        client.cancel_order("order-1", exchange_index=0)
        assert calls[-1][2]["params"] == {"exchange_index": 0}


@pytest.mark.parametrize("status", [429, 503, 302])
def test_errors_never_replay_writes(monkeypatch, status):
    calls, _ = capture_requests(monkeypatch, response(status))
    exception = orders.OrderOutcomeUnknown if status >= 500 or status == 302 else requests.HTTPError
    with pytest.raises(exception):
        orders.OrderClient("predictions").create_order(PREDICTION, **FIELDS)
    assert len(calls) == 1


def test_transport_uncertainty_preserves_intent_and_does_not_retry(monkeypatch):
    failure = requests.Timeout("timeout")
    calls, _ = capture_requests(monkeypatch)

    def fail(method, url, **kwargs):
        calls.append(kwargs)
        raise failure

    monkeypatch.setattr(orders.kalshi_rest._public_session, "request", fail)
    with pytest.raises(orders.OrderOutcomeUnknown, match="intent-1.*reconcile") as exc:
        orders.OrderClient("perps").create_order(PERP, **FIELDS)
    assert exc.value.__cause__ is failure
    assert len(calls) == 1 and calls[0]["json"]["client_order_id"] == "intent-1"


def test_malformed_success_is_unknown_not_permission_to_resubmit(monkeypatch):
    result = response()
    result._content = b"{}"
    calls, _ = capture_requests(monkeypatch, result)
    with pytest.raises(orders.OrderOutcomeUnknown):
        orders.OrderClient("perps").create_order(PERP, **FIELDS)
    assert len(calls) == 1
