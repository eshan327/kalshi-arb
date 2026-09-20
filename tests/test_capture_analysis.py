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

    decisions, summary = analyze(events)

    assert len(decisions) == 1
    assert decisions[0]["best_edge_cents"] == 7
    assert summary["markets"] == 1
    assert summary["buy_signals"] == 1
    assert summary["horizon_coverage"][-1]["decisions"] == 1
    assert summary["sequence_gaps"] == [
        {
            "kind": "orderbook_delta",
            "sid": 4,
            "first_seq": 10,
            "last_seq": 12,
            "missing_sequence_numbers": 1,
        }
    ]
