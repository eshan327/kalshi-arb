from research.capture_analysis import analyze


def test_capture_analysis_summarizes_decisions_and_sequence_gaps():
    events = [
        {
            "receipt_ts": 100.0,
            "kind": "orderbook_delta",
            "sid": 4,
            "seq": 10,
            "market_ticker": "TEST",
            "seconds_to_expiry": 30.0,
            "payload": {},
        },
        {
            "receipt_ts": 100.2,
            "kind": "orderbook_delta",
            "sid": 4,
            "seq": 12,
            "market_ticker": "TEST",
            "seconds_to_expiry": 29.8,
            "payload": {},
        },
        {
            "receipt_ts": 100.25,
            "kind": "trade",
            "sid": 5,
            "seq": 20,
            "market_ticker": "TEST",
            "seconds_to_expiry": 29.75,
            "payload": {
                "trade_id": "trade-1",
                "yes_price_dollars": "0.6200",
                "no_price_dollars": "0.3800",
                "count_fp": "3.00",
                "taker_outcome_side": "yes",
                "taker_book_side": "bid",
                "is_block_trade": False,
                "ts_ms": 100250,
            },
        },
        {
            "receipt_ts": 100.26,
            "kind": "order_submission",
            "sid": None,
            "seq": None,
            "market_ticker": "TEST",
            "seconds_to_expiry": 29.74,
            "payload": {
                "client_order_id": "kalshi-algo-1",
                "execution_mode": "live",
                "side": "yes",
                "action": "buy",
                "count": 2,
                "price_cents": 62,
            },
        },
        {
            "receipt_ts": 100.28,
            "kind": "fill",
            "sid": 6,
            "seq": None,
            "market_ticker": "TEST",
            "seconds_to_expiry": 29.72,
            "payload": {
                "client_order_id": "kalshi-algo-1",
                "trade_id": "own-fill-1",
                "count_fp": "2.00",
            },
        },
        {
            "receipt_ts": 100.29,
            "kind": "order_result",
            "sid": None,
            "seq": None,
            "market_ticker": "TEST",
            "seconds_to_expiry": 29.71,
            "payload": {
                "client_order_id": "kalshi-algo-1",
                "result": {
                    "order": {
                        "order_id": "order-1",
                        "fill_count": "2.00",
                    }
                },
            },
        },
        {
            "receipt_ts": 100.3,
            "kind": "strategy_decision",
            "sid": None,
            "seq": None,
            "market_ticker": "TEST",
            "seconds_to_expiry": 29.7,
            "payload": {
                "reason": "ev_signal_ready",
                "execution_mode": "paper",
                "armed": True,
                "pricing": {
                    "p_model": 0.7,
                    "sigma_annual": 0.5,
                    "vol_window_seconds": 600,
                    "vol_window_policy": "pre_settlement",
                },
                "diagnostics": {
                    "yes_bid_cents": 60,
                    "yes_ask_cents": 61,
                    "edge_yes_cents": 7,
                    "required_taker_edge_cents": 2,
                },
                "signal": {
                    "action": "buy",
                    "side": "yes",
                    "count": 2,
                    "quote_price_cents": 62,
                    "edge_cents": 6,
                },
            },
        },
    ]

    decisions, trades, orders, summary = analyze(events)

    assert len(decisions) == 1
    assert len(trades) == 1
    assert trades[0]["trade_id"] == "trade-1"
    assert trades[0]["taker_outcome_side"] == "yes"
    assert len(orders) == 1
    assert orders[0]["client_order_id"] == "kalshi-algo-1"
    assert orders[0]["response_latency_ms"] == 30.0
    assert orders[0]["first_fill_latency_ms"] == 20.0
    assert decisions[0]["best_edge_cents"] == 7
    assert summary["markets"] == 1
    assert summary["buy_signals"] == 1
    assert summary["public_trades"] == 1
    assert summary["non_block_public_trades"] == 1
    assert summary["orders_submitted"] == 1
    assert summary["orders_with_fill"] == 1
    assert summary["median_order_response_latency_ms"] == 30.0
    assert summary["median_first_fill_latency_ms"] == 20.0
    assert summary["horizon_coverage"][-1]["decisions"] == 1
    assert summary["horizon_coverage"][-1]["public_trades"] == 1
    assert summary["horizon_coverage"][-1]["public_trade_contracts"] == 3.0
    assert summary["sequence_gaps"] == [
        {
            "kind": "orderbook_delta",
            "sid": 4,
            "first_seq": 10,
            "last_seq": 12,
            "missing_sequence_numbers": 1,
        }
    ]
