from core.market_metadata import extract_settlement_decimals, extract_suggested_strike
from engine import live_pricing


def test_strike_parser_uses_structured_exchange_terms() -> None:
    assert extract_suggested_strike({"floor_strike": "123456.78"}) == 123_456.78
    assert extract_suggested_strike({"title": "Bitcoin on Sep 8, 2026"}) is None
    assert extract_settlement_decimals({"custom_strike": {"round_digits": "7"}}, 2) == 7


def test_live_pricing_cache_is_single_entry_and_returns_copies(monkeypatch) -> None:
    calls = 0

    def compute_pricing_snapshot(**_kwargs):
        nonlocal calls
        calls += 1
        return {"calls": calls}

    monkeypatch.setattr(
        live_pricing, "compute_pricing_snapshot", compute_pricing_snapshot
    )
    monkeypatch.setattr(
        live_pricing,
        "get_index_state",
        lambda: {"price": 100_000.0, "asset": "BTC"},
    )
    monkeypatch.setattr(live_pricing, "get_index_tick_version", lambda: 1)
    monkeypatch.setattr(live_pricing, "get_index_ticks", lambda **_kwargs: [])
    monkeypatch.setattr(live_pricing.time, "time", lambda: 1_000.0)
    live_pricing.reset_live_pricing_for_new_market()

    kwargs = {
        "strike": 100_000.0,
        "market_ticker": "TEST",
        "close_time_iso": "2026-01-01T00:00:00Z",
    }
    first = live_pricing.compute_live_pricing_snapshot(**kwargs)
    first["mutated"] = True
    second = live_pricing.compute_live_pricing_snapshot(**kwargs)

    assert calls == 1
    assert second == {"calls": 1}
