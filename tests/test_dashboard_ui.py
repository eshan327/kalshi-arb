import pytest

import ui.app as dashboard_app
from core.auth import get_dashboard_token, verify_dashboard_token
from ui.app import (
    DashboardState,
    _book_rows,
    _book_summary,
    _decision_state,
    _execution_summary,
    _position_rows,
    _price,
    _probability_domain,
    _yes_midpoint_probability,
    index_history,
    market_ticks,
    recent_rows,
)


def test_dashboard_requires_operator_authentication(monkeypatch) -> None:
    token = "t" * 32
    monkeypatch.setenv("KALSHI_DASHBOARD_TOKEN", token)
    state = DashboardState(_reflex_internal_init=True)

    state.load()
    guarded_calls = [
        lambda: state.refresh(""),
        lambda: state.choose_mode("live"),
        state.start,
        state.stop,
        state.flatten,
        state.submit_manual,
        state.save_settings,
        state.reset_settings,
    ]
    for call in guarded_calls:
        with pytest.raises(PermissionError, match="Operator authentication required"):
            call()

    state.operator_token = "wrong"
    state.authenticate()
    assert state._operator_authenticated is False

    monkeypatch.setattr(DashboardState, "_refresh", lambda self: None)
    state.operator_token = token
    state.authenticate()
    assert state._operator_authenticated is True
    assert state.operator_token == ""

    calls = []
    monkeypatch.setattr(
        dashboard_app,
        "control_trading",
        lambda operation, mode="": calls.append((operation, mode)),
    )
    state.start()
    assert calls == [("start", "paper")]


def test_dashboard_token_is_strong_and_compared_server_side(monkeypatch) -> None:
    monkeypatch.setenv("KALSHI_DASHBOARD_TOKEN", "short")
    with pytest.raises(ValueError, match="at least 32 characters"):
        get_dashboard_token()
    assert verify_dashboard_token("short") is False

    token = "s" * 32
    monkeypatch.setenv("KALSHI_DASHBOARD_TOKEN", token)
    assert verify_dashboard_token(token) is True
    assert verify_dashboard_token(f"{token}x") is False

    unicode_token = "é" * 32
    monkeypatch.setenv("KALSHI_DASHBOARD_TOKEN", unicode_token)
    assert verify_dashboard_token(unicode_token) is True


def test_asset_prices_preserve_material_decimals() -> None:
    assert _price("0.521500") == "$0.5215"
    assert _price(60_000) == "$60,000"


def test_dashboard_uses_official_averages_without_recalculating():
    history = index_history(
        [
            {"ts": 1, "price": 100, "average": 99.0},
            {"ts": 31, "price": 110, "average": 102.0},
            {"ts": 62, "price": 130},
        ]
    )
    assert [p["average"] for p in history] == [99.0, 102.0, None]
    assert [p["spot"] for p in history] == [100, 110, 130]


def test_dashboard_history_starts_at_current_market_open() -> None:
    rows = [
        {"ts": 99, "price": 100},
        {"ts": 100, "price": 101},
        {"ts": 999, "price": 102},
    ]

    assert market_ticks(rows, "1970-01-01T00:16:40+00:00") == rows[1:]


def test_chart_window_preserves_vendor_average():
    rows = [
        {"ts": 100, "price": 100, "average": 95},
        {"ts": 200, "price": 110, "average": 101},
        {"ts": 300, "price": 120, "average": 117},
    ]
    recent = recent_rows(rows, 150)
    assert recent == rows[1:]
    assert [p["average"] for p in index_history(recent)] == [101, 117]


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
            "ticker": "TEST",
            "side": "YES",
            "contracts": "10",
            "cost_basis": "68.4¢",
            "mark": "71¢",
            "exposure": "$6.84",
            "unrealized_pnl": "$0.26",
            "pnl_tone": "positive",
            "row_class": "position-line active-position",
        }
    ]


def test_market_probability_and_execution_use_exchange_semantics() -> None:
    assert (
        _yes_midpoint_probability({"yes_bids": [[32, 4]], "yes_asks": [[34, 5]]})
        == 0.33
    )
    assert _yes_midpoint_probability({"yes_bids": [[32, 4]]}) is None
    assert (
        _execution_summary(
            {
                "reason": "manual_live_filled",
                "side": "no",
                "action": "buy",
                "count": 2,
                "order": {
                    "fill_count": "1.00",
                    "average_fill_price": "0.6000",
                    "average_fee_paid": "0.0100",
                },
            }
        )
        == "LIVE BUY NO · 1/2 filled · @ 40¢ · fee 1¢/ct"
    )


def test_probability_domain_keeps_low_probability_signals_legible() -> None:
    assert _probability_domain(
        [{"model": 5.5, "market": 3.2}, {"model": 6.1, "market": 3.8}]
    ) == [0.0, 10.0]
    assert _probability_domain([]) == [0, 100]


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
