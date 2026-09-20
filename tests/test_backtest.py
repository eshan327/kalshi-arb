from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.market_profiles import get_market_profile
from engine.asian_pricer import SECONDS_PER_YEAR, _fixing_times_years
from engine.pricing.pipeline import compute_pricing_snapshot
from research.backtest import _settlement_state, normalize_cf_history


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


def test_cf_history_normalization_collapses_subsecond_values():
    rows = [
        {"time": 1_000_100, "value": "100.0"},
        {"time": 1_000_900, "value": "101.0"},
        {"time": 1_001_000, "value": "102.0"},
    ]
    ticks = normalize_cf_history(rows)
    assert ticks == [
        {"ts": 1000.0, "price": 101.0},
        {"ts": 1001.0, "price": 102.0},
    ]


def test_replayed_settlement_window_excludes_start_and_includes_current_fix():
    ticks = [
        {"ts": float(ts), "price": float(ts)}
        for ts in range(940, 1001)
    ]
    state = _settlement_state(ticks, now_ts=970.0, close_ts=1000.0, window=60)
    avg = state["final_average"]
    assert avg["start"] == 940.0
    assert avg["count"] == 30
    assert avg["end"] == 970.0
    assert avg["value"] == pytest.approx(sum(range(941, 971)) / 30)
