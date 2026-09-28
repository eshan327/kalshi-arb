import csv
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

import historical_data
from perps_backtest import Book, _missing_funding, run_backtest


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat()


def test_gold_history_exports_market_terms_without_cf(monkeypatch, tmp_path):
    monkeypatch.setattr(historical_data, "KALSHI_ENV", "prod")
    market = {"ticker": "KXGOLD15M-TEST", "open_time": iso(1000),
              "close_time": iso(1900), "settlement_ts": iso(1901), "result": "yes"}
    monkeypatch.setattr(historical_data, "get_historical_cutoff", lambda: {
        "trades_created_ts": iso(500), "market_settled_ts": iso(500)})
    monkeypatch.setattr(historical_data, "get_settled_markets", lambda *a, **k: [market])
    trade = {"trade_id": "1", "ticker": market["ticker"],
             "created_time": iso(1500), "is_block_trade": False}
    later_trade = {**trade, "trade_id": "2", "created_time": iso(1600)}
    monkeypatch.setattr(historical_data, "get_market_trades", lambda **k: [later_trade, trade])
    candle = {"end_period_ts": 1020, "yes_bid": {"close_dollars": "0.4"}}
    monkeypatch.setattr(historical_data, "get_market_candlesticks", lambda *a, **k: [candle])
    out = tmp_path / "gold"
    result = historical_data.export_15m("GOLD", iso(900), iso(2000), out)
    assert result["markets"] == 1
    assert result["missing_minute_candles"] == 14
    source = json.loads((out / "source.json").read_text())
    assert source["settlement_source"] == "Pyth 1-minute close"
    assert source["kind"] == "market-only"
    assert [json.loads(line)["trade"] for line in (out / source["trades_file"]).read_text().splitlines()] == [trade, later_trade]
    assert json.loads((out / source["candles_file"]).read_text())["candle"] == candle


def test_perps_export_deduplicates_boundaries_and_reports_gaps(monkeypatch, tmp_path):
    monkeypatch.setattr(historical_data, "KALSHI_ENV", "prod")
    candle = {"end_period_ts": 1080, "bid": {"close": "99"},
              "ask": {"close": "100"}, "price": {"close": "99.5"}}
    def get(path, **params):
        if path == "/markets/KXGOLDPERP":
            return {"market": {"ticker": "KXGOLDPERP", "contract_size": "1"}}
        if path.endswith("candlesticks"):
            return {"ticker": "KXGOLDPERP", "candlesticks": [candle]}
        return {"funding_rates": []}
    monkeypatch.setattr(historical_data, "get_perps", get)
    trades = [{"trade_id": "1", "ticker": "KXGOLDPERP", "created_time": iso(1100)},
              {"trade_id": "2", "ticker": "KXGOLDPERP", "created_time": iso(1050)}]
    monkeypatch.setattr(historical_data, "get_perps_pages", lambda *a, **k: trades)
    out = tmp_path / "perps"
    result = historical_data.export_perps("KXGOLDPERP", iso(1020), iso(1140), out)
    assert result["candles"] == 1
    assert result["missing_minute_candles"] == 1
    assert json.loads((out / "candles.jsonl").read_text())["end_period_ts"] == 1080
    assert [json.loads(line)["trade_id"] for line in (out / "trades.jsonl").read_text().splitlines()] == ["2", "1"]


def test_perps_replay_uses_arrival_book_and_funding(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    (history / "manifest.json").write_text(json.dumps({
        "ticker": "KXBTCPERP", "start": iso(1000), "end": iso(2000),
        "environment": "prod", "market": {"asset_class": "Crypto", "tick_size": "0.0001"}}))
    (history / "funding_rates.jsonl").write_text(json.dumps({
        "market_ticker": "KXBTCPERP", "funding_time": iso(1250),
        "funding_rate": "0.01", "mark_price": "100"}) + "\n")
    capture = tmp_path / "capture.jsonl"
    def row(ts, kind, seq=None, **msg):
        if kind.startswith("orderbook"):
            msg["market_ticker"] = "KXBTCPERP"
        return {"received_ns": round(ts * 1e9), "session": "s1",
                "data": {"type": kind, "sid": 1, "seq": seq, "msg": msg}}
    capture.write_text("".join(json.dumps(item) + "\n" for item in [
        row(1090, "session_start"),
        row(1100.1, "orderbook_snapshot", 1, bid=[["99", "2"]], ask=[["100", "2"]]),
        row(1200, "orderbook_delta", 2, side="ask", price="100", delta="-1"),
    ]))
    signals = tmp_path / "signals.csv"
    with signals.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "entry_ts", "exit_ts", "side",
            "contracts", "entry_limit_dollars", "exit_limit_dollars"])
        writer.writeheader()
        writer.writerow({"ticker": "KXBTCPERP", "entry_ts": 1100, "exit_ts": 1300,
                         "side": "long", "contracts": "1", "entry_limit_dollars": "100",
                         "exit_limit_dollars": "99"})
    result = run_backtest(history_dir=history, capture_path=capture, signals_path=signals,
                          output_dir=tmp_path / "out", latency_ms=200,
                          max_book_age_ms=300000, depth_fraction=Decimal("1"),
                          taker_fee_rate=Decimal("0.001"))
    assert result["completed_round_trips"] == 1
    assert float(list(csv.DictReader((tmp_path / "out" / "round_trips.csv").open()))[0]["exit_arrival_ts"]) == 1300.2
    assert Decimal(result["net_pnl_dollars"]) == Decimal("-2.199")
    assert result["open_positions_at_signal_exit"] == 0
    # Funding at the decision time is not owed if the entry arrives later.
    (history / "funding_rates.jsonl").write_text(json.dumps({
        "market_ticker": "KXBTCPERP", "funding_time": iso(1100.1),
        "funding_rate": "0.01", "mark_price": "100"}) + "\n")
    late_entry = run_backtest(history_dir=history, capture_path=capture,
        signals_path=signals, output_dir=tmp_path / "late-entry", latency_ms=200,
        max_book_age_ms=300000, depth_fraction=Decimal("1"),
        taker_fee_rate=Decimal("0.001"))
    assert Decimal(late_entry["net_pnl_dollars"]) == Decimal("-1.199")
    signals.write_text(signals.read_text().replace(",99\n", ",101\n"))
    open_position = run_backtest(history_dir=history, capture_path=capture,
        signals_path=signals, output_dir=tmp_path / "open-position", latency_ms=200,
        max_book_age_ms=300000, depth_fraction=Decimal("1"),
        taker_fee_rate=Decimal("0.001"))
    assert open_position["open_positions_at_signal_exit"] == 1
    assert open_position["completed_round_trips"] == 0
    assert list(csv.DictReader((tmp_path / "open-position" / "round_trips.csv").open()))[0]["open_contracts"] == "1"
    with signals.open("a") as handle:
        handle.write("KXBTCPERP,1400,1500,long,1,100,99\n")
    blocked = run_backtest(history_dir=history, capture_path=capture,
        signals_path=signals, output_dir=tmp_path / "blocked", latency_ms=200,
        max_book_age_ms=300000, depth_fraction=Decimal("1"),
        taker_fee_rate=Decimal("0.001"))
    assert blocked["open_positions_at_signal_exit"] == 1
    assert list(csv.DictReader((tmp_path / "blocked" / "round_trips.csv").open()))[1]["status"] == "blocked_by_open_position"
    book = Book("KXBTCPERP")
    book.ingest(row(1100, "orderbook_snapshot", 1, bid=[["99", "2"]], ask=[["100", "2"]]))
    book.ingest(row(1100.5, "orderbook_snapshot", 1, bid=[["1", "2"]], ask=[["2", "2"]]))
    assert max(book.bids) == Decimal("99")
    book.ingest(row(1101, "orderbook_delta", 3, side="bid", price="99", delta="-1"))
    assert book.updated_ns is None
    signals.write_text(signals.read_text().replace(",1500,long", ",2000,long"))
    with pytest.raises(ValueError, match="funding-history bounds"):
        run_backtest(history_dir=history, capture_path=capture, signals_path=signals,
                     output_dir=tmp_path / "outside", latency_ms=200,
                     max_book_age_ms=300000, depth_fraction=Decimal("1"),
                     taker_fee_rate=Decimal("0.001"))


def test_missing_scheduled_funding_blocks_pnl():
    # 2026-09-27 04:00 UTC is midnight in New York.
    event = datetime(2026, 9, 27, 4, tzinfo=UTC).timestamp()
    assert _missing_funding(event - 60, event + 60, "Crypto", set())
    assert not _missing_funding(event - 60, event + 60, "Crypto", {event})
