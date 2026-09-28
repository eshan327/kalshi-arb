import pytest

from pricing import baseline as pipeline
from pricing import live_pricing


def test_volatility_excludes_future_and_subsecond_data_and_rejects_gaps():
    from pricing.vol_estimator import realized_vol_from_price_points

    points = [(float(t), 100 + (t % 2) * 0.01) for t in range(700, 1001)]
    expected = realized_vol_from_price_points(points, now_ts=1000)
    assert expected > 0
    assert (
        realized_vol_from_price_points(
            list(reversed(points)) + [(1001, 10000), (999.8, 10000)], now_ts=1000
        )
        == expected
    )
    assert realized_vol_from_price_points(points[1:], now_ts=1000) is None
    assert (
        realized_vol_from_price_points([(t, 100) for t, _ in points], now_ts=1000) == 0
    )
    with pytest.raises(ValueError, match="Conflicting"):
        realized_vol_from_price_points(points + [(999, 101)], now_ts=1000)


def test_asian_moments_match_discrete_covariance_and_zero_volatility():
    import math

    from pricing.asian_pricer import (
        _fixing_times_years,
        _levy_moment_match_m2,
        prob_levy_tw_binary,
    )

    times = _fixing_times_years(100.4, 60)
    mean, m2 = _levy_moment_match_m2(100, 0.7, times)
    expected = (
        100**2 / 60**2 * sum(math.exp(0.7**2 * min(a, b)) for a in times for b in times)
    )
    assert mean == 100
    assert m2 == pytest.approx(expected)
    assert prob_levy_tw_binary(100, 99, 0, 100).p_model > 0.999999
    assert prob_levy_tw_binary(100, 101, 0, 100).p_model < 0.000001


def test_live_and_historical_use_identical_baseline_state(monkeypatch):
    from datetime import UTC, datetime

    from backtest import _settlement_state, pricing_at
    from core.markets import get_market_profile

    now, close = 1970.4, 2000.0
    fixes = [{"ts": float(t), "price": 100 + (t % 2) * 0.01} for t in range(1500, 2001)]
    spots = sorted(fixes + [{"ts": now, "price": 100.03}], key=lambda t: t["ts"])
    market = {
        "ticker": "TEST",
        "close_time": datetime.fromtimestamp(close, UTC).isoformat(),
    }
    state = _settlement_state(fixes, now_ts=now, close_ts=close, window=60, spot_ts=now)
    state.update(asset="BTC", price=100.03)
    monkeypatch.setattr(live_pricing, "get_index_state", lambda: state)
    monkeypatch.setattr(live_pricing, "get_index_ticks", lambda: fixes)
    monkeypatch.setattr(pipeline.time, "time", lambda: now)
    live = live_pricing.compute_live_pricing_snapshot(
        strike=100,
        market_ticker="TEST",
        close_time_iso=market["close_time"],
        settlement_decimals=2,
    )
    replay = pricing_at(
        profile=get_market_profile("BTC"),
        asset="BTC",
        market=market,
        strike=100,
        decimals=2,
        eval_ts=now,
        spot_ticks=spots,
        fix_ticks=fixes,
    )
    assert live == replay
    assert live["ready"] and live["twap_samples_observed"] == 30
    now += 0.01
    fresh = live_pricing.compute_live_pricing_snapshot(
        strike=100, market_ticker="TEST", close_time_iso=market["close_time"],
        settlement_decimals=2,
    )
    assert fresh["ready"]
    assert fresh["seconds_to_expiry"] == pytest.approx(close - now)
    monkeypatch.setattr(live_pricing, "get_index_ticks", lambda: [])
    assert live_pricing.compute_live_pricing_snapshot(
        strike=100, market_ticker="TEST", close_time_iso=market["close_time"],
    )["reason"] == "volatility_unavailable"
    state["connected"] = False
    assert live_pricing.compute_live_pricing_snapshot(
        strike=100, market_ticker="TEST", close_time_iso=market["close_time"],
    )["reason"] == "index_disconnected"
