from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backtest import (
    _settlement_state,
    evaluate_market,
    fetch_cf_feeds,
    normalize_cf_history,
    one_second_boundary_ticks,
)
from core.markets import get_market_profile
from data import kalshi_rest
from pricing.asian_pricer import SECONDS_PER_YEAR, _fixing_times_years


def test_fixing_grid_matches_kalshi_open_left_close_right_window():
    times = _fixing_times_years(120.0, 60)
    seconds = [round(value * SECONDS_PER_YEAR) for value in times]
    assert seconds[0] == 61
    assert seconds[-1] == 120
    assert len(seconds) == 60


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


def test_cf_history_hour_windows_deduplicate_boundary(monkeypatch):
    import backtest

    hour = 1_700_000_000 // 3600 * 3600
    calls = 0

    def history(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        seconds = (hour + 3599, hour + 3600) if calls == 1 else (
            hour + 3600, hour + 3601
        )
        return [{"time": ts * 1000, "value": "100"} for ts in seconds]

    monkeypatch.setattr(backtest, "get_cfbenchmarks_history", history)
    monkeypatch.setattr(backtest.time, "sleep", lambda *_: None)
    assert backtest.fetch_cf_range("BRTI", hour + 3599, hour + 3601) == [
        {"ts": float(ts), "price": 100.0}
        for ts in (hour + 3599, hour + 3600, hour + 3601)
    ]


def test_replayed_settlement_window_excludes_start_and_includes_current_fix():
    ticks = [{"ts": float(ts), "price": float(ts)} for ts in range(940, 1001)]
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


def test_high_frequency_replay_derives_live_feed_split_from_historical_ticks(
    monkeypatch,
):
    import backtest

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

    quoted = evaluate_market(
        market,
        spot_ticks=fixes,
        fix_ticks=fixes,
        **{**kwargs, "quotes": [
            {"ts": row.eval_ts - 1, "yes_bid_cents": 40, "yes_ask_cents": 42},
            {"ts": row.eval_ts + 1, "yes_bid_cents": 90, "yes_ask_cents": 92},
        ]},
    )[0]
    assert (quoted.yes_bid_cents, quoted.yes_ask_cents, quoted.quote_age_seconds) == (40, 42, 1)
    invalidated = evaluate_market(
        market,
        spot_ticks=fixes,
        fix_ticks=fixes,
        **{**kwargs, "quotes": [
            {"ts": row.eval_ts - 1, "yes_bid_cents": 40, "yes_ask_cents": 42},
            {"ts": row.eval_ts - 0.5, "yes_bid_cents": None, "yes_ask_cents": None},
        ]},
    )[0]
    assert invalidated.yes_bid_cents is None
    assert invalidated.quote_age_seconds is None


def test_chronological_groups_and_paired_scoring():
    from dataclasses import replace

    from backtest import chronological_split, summarize

    fixes = [{"ts": float(t), "price": 100 + (t % 2) * 0.02} for t in range(1500, 2001)]
    market = {
        "ticker": "TEST",
        "close_time": datetime.fromtimestamp(2000, UTC).isoformat(),
        "strike_price": 100,
        "result": "yes",
    }
    row = evaluate_market(
        market, asset="BTC", spot_ticks=fixes, fix_ticks=fixes, horizons=(30,)
    )[0]
    rows = [
        replace(
            row,
            market_ticker=str(i),
            close_ts=2000 + i * 900,
            eval_ts=1970 + i * 900,
            p_market=0.5 if i == 4 else None,
        )
        for i in range(5)
    ]
    train, holdout = chronological_split(list(reversed(rows)))
    assert len(train) == 4 and len(holdout) == 1
    assert max(r.close_ts for r in train) < min(r.close_ts for r in holdout)
    summary = summarize(rows)
    assert summary["baseline"]["observations"] == 5
    assert (
        summary["paired_baseline"]["observations"]
        == summary["paired_market"]["observations"]
        == 1
    )
    assert summary["paired_market"]["brier"] == 0.25


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


def test_missing_final_fix_never_becomes_known_settlement_data():
    fixes = [
        {"ts": float(t), "price": 100 + (t % 2) * 0.01}
        for t in range(1500, 2001)
        if t != 1950
    ]
    market = {
        "ticker": "TEST",
        "close_time": datetime.fromtimestamp(2000, UTC).isoformat(),
        "strike_price": 100,
        "result": "yes",
    }
    failures = {}
    rows = evaluate_market(
        market,
        asset="BTC",
        spot_ticks=fixes,
        fix_ticks=fixes,
        horizons=(30,),
        failure_counts=failures,
    )
    assert rows == [] and failures
    state = _settlement_state(
        fixes, now_ts=1970.4, close_ts=2000, window=60, spot_ts=1970.4
    )
    assert "final_average" not in state


def test_settled_market_history_merges_live_and_archive_tiers(monkeypatch):
    calls = []

    def pages(path, key, params):
        calls.append((path, key, params))
        if path == "/markets":
            return [
                {"ticker": "RECENT", "result": "yes"},
                {"ticker": "DUP", "result": "no", "source": "live"},
            ]
        if path == "/historical/markets":
            return [
                {"ticker": "OLD", "result": "yes"},
                {"ticker": "DUP", "result": "no", "source": "archive"},
            ]
        raise AssertionError(path)

    monkeypatch.setattr(kalshi_rest, "_public_pages", pages)
    rows = kalshi_rest.get_settled_markets("KXBTC15M")
    by_ticker = {row["ticker"]: row for row in rows}

    assert set(by_ticker) == {"RECENT", "OLD", "DUP"}
    # Live copy wins if a moving cutoff briefly exposes the same market in both.
    assert by_ticker["DUP"]["source"] == "live"
    assert calls[0][0] == "/markets"
    assert calls[0][2] == {"series_ticker": "KXBTC15M", "status": "settled"}
    assert calls[1][0] == "/historical/markets"

    calls.clear()
    kalshi_rest.get_settled_markets(
        "KXBTC15M", min_close_ts=200, archival_cutoff_ts=100
    )
    assert calls == [
        ("/markets", "markets", {
            "series_ticker": "KXBTC15M", "status": "settled", "min_settled_ts": 199
        })
    ]


def test_trades_merge_both_tiers_without_duplicate_fills(monkeypatch):
    calls = []

    def pages(path, key, params):
        calls.append((path, params))
        return [{"trade_id": path}, {"trade_id": "duplicate"}]

    monkeypatch.setattr(kalshi_rest, "_public_pages", pages)
    rows = kalshi_rest.get_market_trades(ticker="TEST", min_ts=1, max_ts=2)
    assert len(rows) == 3
    assert {p for p, _ in calls} == {"/historical/trades", "/markets/trades"}
    assert all(params["is_block_trade"] == "false" for _, params in calls)


def test_trade_history_uses_its_own_cutoff(monkeypatch):
    calls = []
    monkeypatch.setattr(
        kalshi_rest,
        "_public_pages",
        lambda path, key, params: calls.append(path) or [],
    )
    kalshi_rest.get_market_trades(ticker="TEST", min_ts=1, max_ts=2, cutoff_ts=10)
    assert calls == ["/historical/trades"]
    calls.clear()
    kalshi_rest.get_market_trades(ticker="TEST", min_ts=11, max_ts=12, cutoff_ts=10)
    assert calls == ["/markets/trades"]


def test_captured_book_replay_discards_sequence_gaps(tmp_path):
    import json
    from data.capture import load_quote_tapes

    path = tmp_path / "feed.jsonl"
    messages = [
        {"type": "orderbook_snapshot", "seq": 1, "msg": {"market_ticker": "TEST", "yes_dollars_fp": [["0.40", "1.00"]], "no_dollars_fp": [["0.50", "1.00"]]}},
        {"type": "orderbook_delta", "seq": 3, "msg": {"market_ticker": "TEST", "side": "yes", "price_dollars": "0.41", "delta_fp": "1.00"}},
        {"type": "orderbook_delta", "seq": 4, "msg": {"market_ticker": "TEST", "side": "yes", "price_dollars": "0.42", "delta_fp": "1.00"}},
        {"type": "orderbook_snapshot", "seq": 5, "msg": {"market_ticker": "TEST", "yes_dollars_fp": [["0.43", "1.00"]], "no_dollars_fp": [["0.50", "1.00"]]}},
        {"type": "session_end"},
    ]
    path.write_text("".join(json.dumps({"session": "s1", "received_ns": (100 + i) * 1_000_000_000, "data": message}) + "\n" for i, message in enumerate(messages)))
    quotes = load_quote_tapes(path)["TEST"]
    assert [(q["ts"], q["yes_bid_cents"]) for q in quotes] == [
        (100, 40), (101, None), (103, 43), (104, None)
    ]
