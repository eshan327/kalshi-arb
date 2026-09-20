from datetime import UTC, datetime

import pytest

from core.market_profiles import get_market_profile
from engine.asian_pricer import prob_collapsed_variance_binary
from engine.pricing import pipeline


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
        ticks=[{"ts": 1200.0, "price": 95.16}],
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
        ticks=[{"ts": 1970.0, "price": 100.0}],
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
        == "incomplete_settlement_average"
    )
    kwargs["index_state"]["final_average"]["count"] = 60
    assert (
        pipeline.compute_pricing_snapshot(**kwargs)["reason"]
        == "invalid_settlement_average"
    )
    kwargs["index_state"]["connected"] = False
    assert pipeline.compute_pricing_snapshot(**kwargs)["reason"] == "index_disconnected"


def test_horizon_aware_volatility_window_switches_before_settlement() -> None:
    now = 1_000.0
    ticks = [
        {"ts": now - i, "price": 100.0 + (0.02 if i % 2 else -0.02)}
        for i in range(700)
    ]
    common = dict(
        profile=get_market_profile("BTC"),
        feed_asset="BTC",
        spot=100.0,
        ticks=ticks,
        strike=100.0,
        market_ticker="TEST",
        settlement_decimals=2,
        index_state={"connected": True, "timestamp": now},
        now_ts=now,
        vol_window_seconds=300,
        pre_settlement_vol_window_seconds=600,
        pre_settlement_until_seconds=60,
    )
    early = pipeline.compute_pricing_snapshot(
        **common,
        close_time_iso=datetime.fromtimestamp(now + 120, UTC).isoformat(),
    )
    assert early["ready"]
    assert early["vol_window_seconds"] == 600
    assert early["vol_window_policy"] == "pre_settlement"

    late_state = {
        "connected": True,
        "timestamp": now,
        "average_ts": now,
        "final_average": {
            "start": now - 30,
            "end": now,
            "count": 30,
            "value": 100.0,
        },
    }
    late = pipeline.compute_pricing_snapshot(
        **{**common, "index_state": late_state},
        close_time_iso=datetime.fromtimestamp(now + 30, UTC).isoformat(),
    )
    assert late["ready"]
    assert late["vol_window_seconds"] == 300
    assert late["vol_window_policy"] == "base"
