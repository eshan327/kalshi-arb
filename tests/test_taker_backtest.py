import csv
import json
from datetime import UTC, datetime
from decimal import Decimal

from data.orderbook import OrderBook
from taker_backtest import run_backtest, walk_asks


def test_taker_replay_uses_arrival_depth_partial_fills_and_sequence_gaps(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    markets = [
        {"ticker": ticker, "close_time": datetime.fromtimestamp(close, UTC).isoformat(), "result": result}
        for ticker, close, result in [("A", 1100, "yes"), ("B", 1200, "no"), ("C", 1200, "yes")]
    ]
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
        return {"received_ns": round(ts * 1e9), "session": "s1", "data": {"type": kind, "seq": seq, "msg": msg}}
    with capture.open("w") as handle:
        for item in [
            row(999.0, "session_start"),
            row(1000.05, "orderbook_snapshot", "A", 1, yes_dollars_fp=[["0.35", "5"]], no_dollars_fp=[["0.42", "1"], ["0.45", "2"]]),
            row(1000.05, "orderbook_snapshot", "B", 1, yes_dollars_fp=[["0.35", "5"]], no_dollars_fp=[["0.60", "1"]]),
            row(1000.05, "orderbook_snapshot", "C", 1, yes_dollars_fp=[["0.35", "5"]], no_dollars_fp=[["0.42", "1"]]),
            row(1000.08, "orderbook_delta", "C", 3, side="no", price_dollars="0.42", delta_fp="1"),
            row(1000.15, "orderbook_delta", "A", 2, side="no", price_dollars="0.42", delta_fp="-1"),
        ]:
            handle.write(json.dumps(item) + "\n")
    result = run_backtest(input_dir=source, capture_path=capture, signals_path=signals,
                          output_dir=tmp_path / "out", latency_ms=100)
    with (tmp_path / "out" / "fills.csv").open(newline="") as handle:
        fills = {row["market_ticker"]: row for row in csv.DictReader(handle)}
    assert fills["A"]["status"] == "partial"
    assert Decimal(fills["A"]["filled_contracts"]) == 3
    assert Decimal(fills["A"]["cost_dollars"]) == Decimal("1.32")
    assert Decimal(fills["A"]["fee_dollars"]) == Decimal("0.06")
    assert Decimal(fills["A"]["pnl_dollars"]) == Decimal("1.62")
    assert fills["B"]["status"] == "no_depth_within_limit"
    assert fills["C"]["status"] == "no_continuous_book"
    assert result["train"]["filled_orders"] == 1
    assert result["holdout"]["filled_orders"] == 0
    late = run_backtest(input_dir=source, capture_path=capture, signals_path=signals,
                        output_dir=tmp_path / "late", latency_ms=200)
    assert Decimal(late["all"]["filled_contracts"]) == 2

    book = OrderBook("NO")
    book.load_ws_snapshot({"yes_dollars_fp": [["0.35", "2"]],
                           "no_dollars_fp": [["0.42", "1"]]}, 1)
    filled, cost, _, levels = walk_asks(book, "no", Decimal("1"), Decimal("65"), Decimal("1"))
    assert filled == 1 and cost == Decimal("0.65")
    assert levels[0]["price_cents"] == "65.0"
