from datetime import UTC, datetime

import pytest

from core.market_profiles import get_market_profile
from engine.asian_pricer import prob_collapsed_variance_binary
from engine.pricing import pipeline
from engine.settlement_sampling import reconstruct_discrete_forward_fill_samples
from engine.trading.strategy import _kelly_target_contracts
from feeds.brti_calc import (
    calculate_brti,
    compute_dynamic_spacing,
    compute_price_volume_curves,
    uncross_consolidated_book,
)


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


def test_settlement_samples_use_start_inclusive_end_exclusive_seconds() -> None:
    points = [(99.0, 9.0), (100.0, 10.0), (101.0, 11.0), (102.0, 12.0), (103.0, 13.0)]
    samples, elapsed = reconstruct_discrete_forward_fill_samples(
        points, 100.0, 103.0, max_staleness_sec=5.0
    )

    assert elapsed == 3
    assert samples == [10.0, 11.0, 12.0]


def test_pricing_is_anchored_to_the_official_opening_reference(monkeypatch) -> None:
    close_ts = 2_000.0
    now_ts = 1_200.0
    ticks = [
        {"ts": float(ts), "brti": 95.0 + (ts - 1_040) * 0.001, "status": "ok"}
        for ts in range(1_040, 1_201)
    ]
    monkeypatch.setattr(pipeline.time, "time", lambda: now_ts)

    snapshot = pipeline.compute_pricing_snapshot(
        profile=get_market_profile("BTC"),
        feed_asset="BTC",
        spot=ticks[-1]["brti"],
        ticks=ticks,
        strike=100.03,
        market_ticker="TEST",
        close_time_iso=datetime.fromtimestamp(close_ts, UTC).isoformat(),
        settlement_decimals=2,
        source_exchanges=3,
    )

    assert snapshot["ready"] is True
    assert snapshot["proxy_anchor_samples"] == 60
    assert snapshot["proxy_basis_adjustment"] == pytest.approx(5.0005)
    assert snapshot["spot_index"] == pytest.approx(100.1605)
    assert snapshot["model_strike_usd"] == pytest.approx(100.025)


def test_pricing_blocks_when_opening_proxy_reference_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(pipeline.time, "time", lambda: 1_200.0)
    snapshot = pipeline.compute_pricing_snapshot(
        profile=get_market_profile("DOGE"),
        feed_asset="DOGE",
        spot=0.2,
        ticks=[{"ts": 1_200.0, "brti": 0.2, "status": "ok"}],
        strike=0.2,
        market_ticker="TEST",
        close_time_iso=datetime.fromtimestamp(2_000, UTC).isoformat(),
        settlement_decimals=7,
        source_exchanges=2,
    )

    assert snapshot["ready"] is False
    assert snapshot["reason"] == "proxy_anchor_unavailable"


def test_dynamic_index_depth_handles_fractional_and_large_asset_sizes() -> None:
    bids = [(100.0, 0.25), (99.0, 0.25)]
    asks = [(101.0, 0.25), (102.0, 0.25)]
    spacing = compute_dynamic_spacing(bids, asks)
    assert spacing == pytest.approx(0.005)
    _, _, mid_pv, _ = compute_price_volume_curves(bids, asks, spacing=spacing)
    assert len(mid_pv) == 100

    books = {
        "COINBASE": {
            "bids": {0.1999: 1_000_000.0},
            "asks": {0.2001: 1_000_000.0},
            "last_update": 100.0,
        },
        "KRAKEN": {
            "bids": {0.1998: 1_000_000.0},
            "asks": {0.2002: 1_000_000.0},
            "last_update": 100.0,
        },
    }
    value, depth, sources = calculate_brti(books, current_time=100.0, price_decimals=6)
    assert value == pytest.approx(0.2, abs=0.0002)
    assert depth > 0
    assert sources == 2


def test_crossed_consolidated_volume_is_removed_and_kelly_uses_fees() -> None:
    bids, asks = uncross_consolidated_book(
        [(101.0, 1.0), (99.0, 2.0)], [(100.0, 0.5), (102.0, 2.0)]
    )
    target, fraction, all_in = _kelly_target_contracts(
        p_win=0.70,
        quote_price_cents=60.0,
        fee_cents=2.0,
        bankroll_cents=10_000,
        max_position_usd=50.0,
    )

    assert bids[0] == pytest.approx((101.0, 0.5))
    assert asks[0] == pytest.approx((102.0, 2.0))
    assert all_in == 62.0
    assert fraction > 0
    assert target == int(min(10_000 * fraction, 5_000) // all_in)


def test_market_profiles_use_only_configured_benchmark_constituents() -> None:
    assert get_market_profile("XRP").exchange_sources == (
        "coinbase",
        "kraken",
        "bitstamp",
    )
    assert get_market_profile("BNB").exchange_sources == ("coinbase", "kraken")
    assert all(
        len(get_market_profile(asset).exchange_sources) >= 2
        for asset in (
            "BTC",
            "ETH",
            "SOL",
            "XRP",
            "DOGE",
            "BNB",
            "ADA",
            "NEAR",
            "BCH",
            "HYPE",
            "TON",
            "ZEC",
        )
    )
