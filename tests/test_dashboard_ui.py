import pytest

import ui.app as dashboard_app
from core.auth import get_dashboard_token, verify_dashboard_token
from ui.app import DashboardState, _execution_summary, _yes_midpoint_probability


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
