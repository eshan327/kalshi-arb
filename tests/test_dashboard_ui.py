from ui.app import (
    _book_rows,
    _book_summary,
    _decision_state,
    _position_rows,
    market_ticks,
    moving_average,
    recent_rows,
)


def test_dashboard_price_history_uses_a_time_window() -> None:
    history = moving_average(
        [
            {"ts": 1, "brti": 100},
            {"ts": 31, "brti": 110},
            {"ts": 62, "brti": 130},
        ],
        60,
    )

    assert [point["average"] for point in history] == [100.0, 105.0, 120.0]
    assert [point["spot"] for point in history] == [100.0, 110.0, 130.0]


def test_dashboard_history_starts_at_current_market_open() -> None:
    rows = [
        {"ts": 99, "brti": 100},
        {"ts": 100, "brti": 101},
        {"ts": 999, "brti": 102},
    ]

    assert market_ticks(rows, "1970-01-01T00:16:40+00:00") == rows[1:]


def test_chart_window_rolls_and_only_average_resets() -> None:
    rows = [
        {"ts": 100, "brti": 100},
        {"ts": 200, "brti": 110},
        {"ts": 300, "brti": 120},
    ]
    recent = recent_rows(rows, 150)
    history = moving_average(recent, 60, average_start_ts=250)

    assert recent == rows[1:]
    assert [point["spot"] for point in history] == [110.0, 120.0]
    assert [point["average"] for point in history] == [None, 120.0]


def test_position_rows_show_cost_and_mark_to_market_pnl() -> None:
    account = {
        "positions": [
            {
                "market_ticker": "TEST",
                "side": "yes",
                "contracts": 10,
                "avg_entry_cents": 68.4,
                "market_exposure_cents": 684,
            }
        ]
    }
    rows = _position_rows(account, {"yes_bids": [[71, 4]]}, "TEST")

    assert rows == [
        {
            "side": "YES",
            "contracts": "10",
            "cost_basis": "68.4¢",
            "mark": "71¢",
            "exposure": "$6.84",
            "unrealized_pnl": "$0.26",
            "pnl_tone": "positive",
        }
    ]


def test_terminal_summaries_make_book_and_decision_explicit() -> None:
    book = {
        "yes_bids": [[68, 4], [67, 2]],
        "yes_asks": [[70, 3], [71, 5]],
    }

    assert _book_summary(book, "yes") == {
        "bid": "68¢",
        "ask": "70¢",
        "spread": "2¢",
    }
    assert [row["row_class"] for row in _book_rows(book, "yes")] == [
        "ask",
        "ask top-of-book",
        "bid top-of-book",
        "bid",
    ]
    assert _decision_state(
        {
            "action_intent": "PASS — edge below minimum",
            "decision_reason": "edge_below_threshold",
        },
        True,
    ) == ("NO TRADE", "Edge is below the required minimum.", "warning")


if __name__ == "__main__":
    test_dashboard_price_history_uses_a_time_window()
    test_dashboard_history_starts_at_current_market_open()
    test_chart_window_rolls_and_only_average_resets()
    test_position_rows_show_cost_and_mark_to_market_pnl()
    test_terminal_summaries_make_book_and_decision_explicit()
