from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.market_profiles import get_market_profile
from engine.asian_pricer import SECONDS_PER_YEAR, _fixing_times_years
from engine.pricing.pipeline import compute_pricing_snapshot
from research.backtest import (
    _settlement_state,
    calibrate_market,
    fetch_cf_feeds,
    normalize_cf_history,
    one_second_boundary_ticks,
    trade_tape_market,
)


def test_fixing_grid_matches_kalshi_open_left_close_right_window():
    times = _fixing_times_years(120.0, 60)
    seconds = [round(value * SECONDS_PER_YEAR) for value in times]
    assert seconds[0] == 61
    assert seconds[-1] == 120
    assert len(seconds) == 60


def test_pricing_snapshot_accepts_explicit_replay_clock():
    now = 1_000.0
    ticks = [
        {"ts": now - 10 + i, "price": 100.0 + (i % 2) * 0.01}
        for i in range(11)
    ]
    snapshot = compute_pricing_snapshot(
        profile=get_market_profile("BTC"),
        feed_asset="BTC",
        spot=ticks[-1]["price"],
        ticks=ticks,
        strike=100.0,
        market_ticker="TEST",
        close_time_iso=datetime.fromtimestamp(now + 120, UTC).isoformat(),
        index_state={"connected": True, "timestamp": now},
        now_ts=now,
    )
    assert snapshot["ready"]
    assert snapshot["seconds_to_expiry"] == pytest.approx(120.0)
    assert not snapshot["vol_is_fallback"]


def test_cf_history_preserves_subsecond_values_and_exact_second_fixes():
    rows = [
        {"time": 1_700_000_000_000, "value": "99.0"},
        {"time": 1_700_000_000_200, "value": "100.0"},
        {"time": 1_700_000_000_800, "value": "101.0"},
        {"time": 1_700_000_001_000, "value": "102.0"},
    ]
    ticks = normalize_cf_history(rows)
    assert ticks == [
        {"ts": 1_700_000_000.0, "price": 99.0},
        {"ts": 1_700_000_000.2, "price": 100.0},
        {"ts": 1_700_000_000.8, "price": 101.0},
        {"ts": 1_700_000_001.0, "price": 102.0},
    ]
    assert one_second_boundary_ticks(ticks) == [
        {"ts": 1_700_000_000.0, "price": 99.0},
        {"ts": 1_700_000_001.0, "price": 102.0},
    ]


def test_replayed_settlement_window_excludes_start_and_includes_current_fix():
    ticks = [
        {"ts": float(ts), "price": float(ts)}
        for ts in range(940, 1001)
    ]
    state = _settlement_state(
        ticks,
        now_ts=970.4,
        close_ts=1000.0,
        window=60,
        spot_ts=970.4,
    )
    avg = state["final_average"]
    assert avg["start"] == 940.0
    assert avg["count"] == 30
    assert avg["end"] == 970.0
    assert avg["value"] == pytest.approx(sum(range(941, 971)) / 30)
    assert state["timestamp"] == 970.4
    assert state["average_ts"] == 970.0


def test_fixed_horizon_calibration_reaches_collapsed_final_minute():
    close = 2_000.0
    # Enough nonconstant 1 Hz history to avoid the volatility fallback.
    fix_ticks = [
        {
            "ts": float(ts),
            "price": 100.0 + (0.02 if ts % 2 else -0.02),
        }
        for ts in range(1_000, 2_001)
    ]
    # Production fast spot can be newer than the latest official second fix.
    spot_ticks = sorted(
        fix_ticks
        + [
            {"ts": 1_970.2, "price": 100.03},
            {"ts": 1_970.4, "price": 100.04},
        ],
        key=lambda row: row["ts"],
    )
    market = {
        "ticker": "TEST",
        "close_time": datetime.fromtimestamp(close, UTC).isoformat(),
        "strike_price": 100.0,
        "result": "yes",
    }
    rows = calibrate_market(
        market,
        asset="BTC",
        spot_ticks=spot_ticks,
        fix_ticks=fix_ticks,
        horizons=(30,),
        subsecond_offset=0.4,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.regime == "collapsed"
    assert row.known_fix_count == 30
    assert row.seconds_to_expiry == pytest.approx(29.6)
    assert row.spot == pytest.approx(100.04)
    assert row.one_second_spot == pytest.approx(99.98)
    assert row.fast_spot_changed_probability



def test_trade_tape_compares_model_with_actual_trade_probability(monkeypatch):
    import research.backtest as backtest

    close = 2_000.0
    market = {
        "ticker": "TEST",
        "open_time": datetime.fromtimestamp(1_100.0, UTC).isoformat(),
        "close_time": datetime.fromtimestamp(close, UTC).isoformat(),
        "strike_price": 100.0,
        "result": "yes",
        "_data_tier": "live",
    }
    monkeypatch.setattr(
        backtest,
        "get_market_trades",
        lambda **kwargs: [
            {
                "trade_id": "trade-1",
                "created_time": datetime.fromtimestamp(1_970.4, UTC).isoformat(),
                "count_fp": "3.00",
                "yes_price_dollars": "0.6000",
                "taker_outcome_side": "yes",
                "taker_book_side": "bid",
            }
        ],
    )
    monkeypatch.setattr(
        backtest,
        "_pricing_at",
        lambda **kwargs: {
            "ready": True,
            "vol_is_fallback": False,
            "p_model": 0.70,
            "regime": "collapsed",
        },
    )

    rows = trade_tape_market(
        market,
        asset="BTC",
        spot_ticks=[{"ts": 1_970.4, "price": 100.0}],
        fix_ticks=[{"ts": 1_970.0, "price": 100.0}],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.count == 3
    assert row.p_market == pytest.approx(0.60)
    assert row.p_model == pytest.approx(0.70)
    assert row.model_minus_market_cents == pytest.approx(10)
    assert row.model_brier < row.market_brier
    assert row.taker_outcome_side == "yes"



def test_high_frequency_replay_derives_live_feed_split_from_historical_ticks(monkeypatch):
    import research.backtest as backtest

    calls = []

    def fake_range(index_id, start_ts, end_ts):
        calls.append((index_id, start_ts, end_ts))
        return [
            {"ts": 1000.0, "price": 100.0},
            {"ts": 1000.2, "price": 100.1},
            {"ts": 1000.4, "price": 100.2},
            {"ts": 1001.0, "price": 100.3},
        ]

    monkeypatch.setattr(backtest, "fetch_cf_range", fake_range)
    spot, fixes, resolution = fetch_cf_feeds(
        get_market_profile("BTC"),
        1000.0,
        1001.0,
    )
    assert len(calls) == 1
    assert fixes == [
        {"ts": 1000.0, "price": 100.0},
        {"ts": 1001.0, "price": 100.3},
    ]
    assert spot[-1]["ts"] == 1001.0
    assert len(spot) == 4
    assert resolution == "PER_200MS"


def test_candle_quote_supports_live_dollar_field_names():
    import research.backtest as backtest

    assert backtest._candle_quote(
        {
            "yes_bid": {"close_dollars": "0.5900"},
            "yes_ask": {"close_dollars": "0.6000"},
        }
    ) == (59.0, 60.0, 41.0)
