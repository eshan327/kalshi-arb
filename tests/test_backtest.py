from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backtest import (
    _settlement_state, chronological_split, evaluate_market,
    normalize_cf_history, one_second_boundary_ticks,
)
from core.markets import parse_iso8601_to_epoch


def test_baseline_no_lookahead_and_market_comparison_at_same_horizon():
    from dataclasses import asdict

    fixes = [{"ts": float(t), "price": 100 + (t % 2) * 0.02} for t in range(1500, 2001)]
    market = {
        "ticker": "TEST",
        "close_time": datetime.fromtimestamp(2000, UTC).isoformat(),
        "strike_price": 100,
        "result": "yes",
    }
    trades = [
        {
            "created_time": datetime.fromtimestamp(t, UTC).isoformat(),
            "yes_price_dollars": p,
        }
        for t, p in [(1969, "0.6"), (1971, "0.9")]
    ]
    kwargs = dict(asset="BTC", horizons=(30,), subsecond_offset=0.4, trades=trades)
    row = evaluate_market(market, spot_ticks=fixes, fix_ticks=fixes, **kwargs)[0]
    past = [t for t in fixes if t["ts"] <= row.eval_ts]
    future = [{"ts": t["ts"], "price": 10000} for t in fixes if t["ts"] > row.eval_ts]
    replay = evaluate_market(
        market, spot_ticks=past + future, fix_ticks=past + future, **kwargs
    )[0]
    assert asdict(row) == asdict(replay)
    assert row.p_market == 0.6
    assert row.market_trade_age_seconds == pytest.approx(1.4)
    stale = evaluate_market(
        market,
        spot_ticks=fixes,
        fix_ticks=fixes,
        **{**kwargs, "max_trade_age_seconds": 1},
    )[0]
    assert stale.p_market is None

    missing = [tick for tick in fixes if tick["ts"] != 1950]
    assert evaluate_market(market, spot_ticks=missing, fix_ticks=missing, **kwargs) == []


def test_baseline_split_purges_unavailable_training_outcome():
    from dataclasses import replace

    fixes = [{"ts": float(t), "price": 100.0} for t in range(1500, 2901)]
    market = {"ticker": "A", "close_time": datetime.fromtimestamp(2000, UTC).isoformat(),
              "settlement_ts": datetime.fromtimestamp(2880, UTC).isoformat(),
              "strike_price": 100, "result": "yes"}
    row = evaluate_market(market, asset="BTC", spot_ticks=fixes, fix_ticks=fixes,
                          horizons=(30,))[0]
    later = replace(row, market_ticker="B", close_ts=2900, outcome_ts=2900,
                    eval_ts=2870)
    train, holdout, crossing = chronological_split([row, later])
    assert train == [] and holdout == [later] and crossing == [row]


def test_cf_fixings_keep_exact_second_grid_and_reject_gaps():
    ticks = normalize_cf_history([
        {"time": 1_700_000_000_000, "value": "100"},
        {"time": 1_700_000_000_200, "value": "101"},
        {"time": 1_700_000_001_000, "value": "102"},
    ])
    assert one_second_boundary_ticks(ticks) == [ticks[0], ticks[2]]
    fixes = [{"ts": float(ts), "price": 100.0} for ts in range(941, 971)]
    state = _settlement_state(fixes, now_ts=970.4, close_ts=1000, window=60)
    assert state["final_average"]["count"] == 30
    assert "final_average" not in _settlement_state(
        fixes[1:], now_ts=970.4, close_ts=1000, window=60
    )
    assert parse_iso8601_to_epoch("2026-09-28T04:45:00") is None


def test_market_history_merges_tiers_and_rejects_conflicting_trades(monkeypatch):
    from data import kalshi_rest

    def pages(path, _key, _params):
        if path == "/markets":
            return [{"ticker": "NEW"}, {"ticker": "DUP", "source": "live"}]
        if path == "/historical/markets":
            return [{"ticker": "OLD"}, {"ticker": "DUP", "source": "archive"}]
        return [{"trade_id": "1", "ticker": "A" if path == "/markets/trades" else "B"}]

    monkeypatch.setattr(kalshi_rest, "_public_pages", pages)
    markets = kalshi_rest.get_settled_markets("KXBTC15M")
    assert {m["ticker"] for m in markets} == {"NEW", "OLD", "DUP"}
    assert next(m for m in markets if m["ticker"] == "DUP")["source"] == "live"
    with pytest.raises(ValueError, match="Conflicting public trade"):
        kalshi_rest.get_market_trades(ticker="A", min_ts=1, max_ts=2)


def test_saved_source_replays_offline_identically(monkeypatch, tmp_path):
    import csv
    import json

    import backtest

    source = tmp_path / "source"
    source.mkdir()
    markets = [
        {
            "ticker": str(close),
            "close_time": datetime.fromtimestamp(close, UTC).isoformat(),
            "strike_price": 100,
            "result": "yes",
        }
        for close in [2000, 2900, 3800]
    ]
    (source / "source.json").write_text(
        json.dumps({"asset": "BTC", "markets": markets, "trades": {}})
    )
    with (source / "cf_ticks.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ts", "price"])
        writer.writeheader()
        writer.writerows(
            {"ts": t, "price": 100 + (t % 2) * 0.02} for t in range(1500, 3801)
        )
    monkeypatch.setattr(
        backtest,
        "get_settled_markets",
        lambda *_: pytest.fail("offline replay called API"),
    )
    out = tmp_path / "out"
    result = backtest.run_backtest(
        asset="BTC", horizons=(30,), input_dir=source, output_dir=out,
        vol_window_seconds=120,
        end_close=datetime.fromtimestamp(3000, UTC).isoformat(),
    )
    again = tmp_path / "again"
    assert (
        backtest.run_backtest(
            asset="BTC", horizons=(30,), input_dir=out, output_dir=again,
            vol_window_seconds=120,
            end_close=datetime.fromtimestamp(3000, UTC).isoformat(),
        )
        == result
    )
    assert (out / "observations.csv").read_text() == (
        again / "observations.csv"
    ).read_text()
    assert result["train"]["baseline"]["observations"] == 1
    assert result["holdout"]["baseline"]["observations"] == 1
    assert result["vol_window_seconds"] == 120
    assert len(json.loads((out / "source.json").read_text())["markets"]) == 2
