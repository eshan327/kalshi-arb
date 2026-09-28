import csv
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from data.capture import iter_capture, load_quote_tapes
from data.orderbook import OrderBook
from taker_backtest import run_backtest, walk_asks


def test_taker_replay_uses_arrival_depth_partial_fills_and_sequence_gaps(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    markets = [
        {"ticker": ticker, "close_time": datetime.fromtimestamp(close, UTC).isoformat(), "result": result}
        for ticker, close, result in [("A", 1100, "yes"), ("B", 1200, "no"), ("C", 1200, "yes")]
    ]
    markets[0]["settlement_ts"] = datetime.fromtimestamp(1105, UTC).isoformat()
    (source / "source.json").write_text(json.dumps({"markets": markets}))
    signals = tmp_path / "signals.csv"
    with signals.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["market_ticker", "decision_ts", "side", "contracts", "limit_price_cents"])
        writer.writeheader()
        writer.writerows([
            {"market_ticker": "A", "decision_ts": "1000", "side": "yes", "contracts": "4", "limit_price_cents": "45"},
            {"market_ticker": "B", "decision_ts": "1000", "side": "yes", "contracts": "1", "limit_price_cents": "50"},
            {"market_ticker": "C", "decision_ts": "1000", "side": "yes", "contracts": "1", "limit_price_cents": "50"},
        ])
    capture = tmp_path / "capture.jsonl"
    def row(ts, kind, ticker=None, seq=None, **msg):
        if ticker is not None:
            msg["market_ticker"] = ticker
        data = {"type": kind, "seq": seq, "msg": msg}
        if kind == "session_start":
            data["book_price_mode"] = "native_bids"
        return {"received_ns": round(ts * 1e9), "session": "s1", "data": data}
    with capture.open("w") as handle:
        for item in [
            row(999.0, "session_start"),
            row(1000.05, "orderbook_snapshot", "A", 1, yes_dollars_fp=[["0.35", "5"]], no_dollars_fp=[["0.58", "1"], ["0.55", "2"]]),
            row(1000.05, "orderbook_snapshot", "B", 1, yes_dollars_fp=[["0.35", "5"]], no_dollars_fp=[["0.40", "1"]]),
            row(1000.05, "orderbook_snapshot", "C", 1, yes_dollars_fp=[["0.35", "5"]], no_dollars_fp=[["0.58", "1"]]),
            row(1000.08, "orderbook_delta", "C", 3, side="no", price_dollars="0.58", delta_fp="1"),
            row(1000.15, "orderbook_delta", "A", 2, side="no", price_dollars="0.58", delta_fp="-1"),
        ]:
            handle.write(json.dumps(item) + "\n")
    result = run_backtest(input_dir=source, capture_path=capture, signals_path=signals,
                          output_dir=tmp_path / "out", latency_ms=100,
                          balance_precision=Decimal("0.01"))
    with (tmp_path / "out" / "fills.csv").open(newline="") as handle:
        fills = {row["market_ticker"]: row for row in csv.DictReader(handle)}
    assert fills["A"]["status"] == "partial"
    assert float(fills["A"]["outcome_ts"]) == 1105
    assert Decimal(fills["A"]["filled_contracts"]) == 3
    assert Decimal(fills["A"]["cost_dollars"]) == Decimal("1.32")
    assert Decimal(fills["A"]["fee_dollars"]) == Decimal("0.06")
    assert Decimal(fills["A"]["pnl_dollars"]) == Decimal("1.62")
    assert fills["B"]["status"] == "no_depth_within_limit"
    assert fills["C"]["status"] == "no_continuous_book"
    assert result["train"]["filled_orders"] == 1
    assert result["holdout"]["filled_orders"] == 0
    assert result["crossing"]["signals"] == 2
    late = run_backtest(input_dir=source, capture_path=capture, signals_path=signals,
                        output_dir=tmp_path / "late", latency_ms=200)
    assert Decimal(late["all"]["filled_contracts"]) == 2

    book = OrderBook("NO")
    book.load_ws_snapshot({"yes_dollars_fp": [["0.35", "2"]],
                           "no_dollars_fp": [["0.42", "1"]]}, 1)
    filled, cost, _, levels = walk_asks(book, "no", Decimal("1"), Decimal("65"), Decimal("1"))
    assert filled == 1 and cost == Decimal("0.65")
    assert levels[0]["price_cents"] == "65.0"


def test_capture_reader_rejects_out_of_order_receipts(tmp_path):
    capture = tmp_path / "capture.jsonl"
    capture.write_text(''.join(json.dumps({
        "received_ns": ns, "session": "s", "data": {"type": "session_start", "book_price_mode": "native_bids"},
    }) + "\n" for ns in (2, 1)))
    with pytest.raises(ValueError, match="monotonic"):
        list(iter_capture(capture))
    with pytest.raises(ValueError, match="monotonic"):
        load_quote_tapes(capture)

    capture.write_text(json.dumps({"received_ns": 1, "session": "s", "data": {"type": "session_start"}}) + "\n")
    with pytest.raises(ValueError, match="older book price mode"):
        load_quote_tapes(capture)


def test_prediction_book_uses_no_bid_for_yes_ask():
    book = OrderBook("A")
    book.load_ws_snapshot({"yes_dollars_fp": [["0.42", "2"]],
                           "no_dollars_fp": [["0.56", "3"]]}, 1)
    assert book.get_best_prices() == (42.0, 44.0, 56.0, 58.0)
    book.apply_delta_with_seq(2, {"side": "no", "price_dollars": "0.56",
                                  "delta_fp": "-1"})
    assert book.no[56.0] == 2.0
    book.apply_delta_with_seq(3, {"side": "no", "price_dollars": "0.56",
                                  "delta_fp": "-3"})
    assert book.needs_resync
