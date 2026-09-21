from datetime import UTC, datetime

import pytest

from core.markets import (
    extract_settlement_decimals,
    extract_suggested_strike,
    get_market_profile,
)
from pricing import baseline as pipeline
from pricing import live_pricing
from pricing.asian_pricer import prob_collapsed_variance_binary


def test_final_minute_probability_is_price_scale_invariant_and_equality_is_yes() -> (
    None
):
    kwargs = {"sigma_annual": 0.7, "n": 60, "k": 30}
    low = prob_collapsed_variance_binary(
        strike=1.0, mean_known_samples=1.001, mu_fwd=1.001, **kwargs
    )
    high = prob_collapsed_variance_binary(
        strike=64_000.0,
        mean_known_samples=64_064.0,
        mu_fwd=64_064.0,
        **kwargs,
    )
    terminal = prob_collapsed_variance_binary(
        strike=100.0,
        sigma_annual=0.7,
        n=60,
        k=60,
        mean_known_samples=100.0,
        mu_fwd=100.0,
    )

    assert low.p_model == pytest.approx(high.p_model, abs=1e-10)
    assert terminal.p_model > 0.999999


def test_official_price_is_not_basis_adjusted(monkeypatch):
    monkeypatch.setattr(pipeline.time, "time", lambda: 1200.0)
    snapshot = pipeline.compute_pricing_snapshot(
        profile=get_market_profile("BTC"),
        feed_asset="BTC",
        spot=95.16,
        ticks=[{"ts": t, "price": 95.16 + (t % 2) * 0.01} for t in range(900, 1201)],
        strike=100.03,
        market_ticker="TEST",
        close_time_iso=datetime.fromtimestamp(2000, UTC).isoformat(),
        index_state={"connected": True, "timestamp": 1200.0},
    )
    assert snapshot["ready"]
    assert snapshot["spot_index"] == 95.16
    assert snapshot["model_strike_usd"] == pytest.approx(100.025)


def test_final_minute_uses_server_sample_count_and_mean(monkeypatch):
    monkeypatch.setattr(pipeline.time, "time", lambda: 1970.0)
    kwargs = dict(
        profile=get_market_profile("BTC"),
        feed_asset="BTC",
        spot=100.0,
        ticks=[{"ts": t, "price": 100.0 + (t % 2) * 0.01} for t in range(1670, 1971)],
        strike=100.0,
        market_ticker="TEST",
        close_time_iso=datetime.fromtimestamp(2000, UTC).isoformat(),
        index_state={
            "connected": True,
            "timestamp": 1970.0,
            "average_ts": 1970.0,
            "final_average": {
                "start": 1940.0,
                "end": 1970.0,
                "count": 30,
                "value": 101.0,
            },
        },
    )
    result = pipeline.compute_pricing_snapshot(**kwargs)
    assert result["ready"]
    assert result["twap_samples_observed"] == 30
    assert result["twap_partial_avg_raw"] == 101.0
    kwargs["index_state"]["final_average"]["start"] = 1040.0
    assert (
        pipeline.compute_pricing_snapshot(**kwargs)["reason"]
        == "settlement_average_unavailable"
    )
    kwargs["index_state"]["final_average"]["start"] = 1940.0
    kwargs["index_state"]["final_average"]["count"] = 12
    assert (
        pipeline.compute_pricing_snapshot(**kwargs)["reason"]
        == "invalid_settlement_average"
    )
    kwargs["index_state"]["final_average"]["count"] = 60
    assert (
        pipeline.compute_pricing_snapshot(**kwargs)["reason"]
        == "invalid_settlement_average"
    )
    kwargs["index_state"]["connected"] = False
    assert pipeline.compute_pricing_snapshot(**kwargs)["reason"] == "index_disconnected"


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


def test_conditional_fixing_times_use_actual_fractional_clock():
    import math

    from pricing.asian_pricer import SECONDS_PER_YEAR

    # One remaining fix: the approximation is the exact one-step GBM probability.
    result = prob_collapsed_variance_binary(
        strike=100.01,
        sigma_annual=0.5,
        n=60,
        k=59,
        mean_known_samples=100,
        mu_fwd=100,
        seconds_to_expiry=0.2,
    )
    required = 60 * 100.01 - 59 * 100
    sigma = 0.5 * math.sqrt(0.2 / SECONDS_PER_YEAR)
    d2 = (math.log(100 / required) - 0.5 * sigma * sigma) / sigma
    expected = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    assert result.p_model == pytest.approx(max(1e-12, expected), abs=1e-10)


def test_baseline_fails_without_history_instead_of_inventing_volatility():
    result = pipeline.compute_pricing_snapshot(
        profile=get_market_profile("BTC"),
        feed_asset="BTC",
        spot=100,
        ticks=[],
        strike=100,
        market_ticker="TEST",
        close_time_iso=datetime.fromtimestamp(2000, UTC).isoformat(),
        index_state={"connected": True, "timestamp": 1000},
        now_ts=1000,
    )
    assert not result["ready"]
    assert result["reason"] == "volatility_unavailable"
    assert result["p_model"] is None


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


def test_strike_parser_uses_structured_exchange_terms() -> None:
    assert extract_suggested_strike({"floor_strike": "123456.78"}) == 123_456.78
    assert extract_suggested_strike({"title": "Bitcoin on Sep 8, 2026"}) is None
    assert extract_settlement_decimals({"custom_strike": {"round_digits": "7"}}, 2) == 7


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
    monkeypatch.setattr(live_pricing, "get_index_tick_version", lambda: 1)
    monkeypatch.setattr(live_pricing.time, "time", lambda: now)
    live_pricing.reset_live_pricing_for_new_market()
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
