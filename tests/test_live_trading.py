import pytest

from engine.orderbook import OrderBook
from engine.trading.fees import expected_value_cents, taker_fee_cents_per_contract
from engine.trading.paper import PaperAccount
from engine.trading.settings import TradingSettings
from engine.trading.strategy import (
    _credit_target_contracts,
    build_trade_signal,
    slipped_price_cents,
)


def test_each_additional_contract_demands_more_credit() -> None:
    assert _credit_target_contracts(credit_cents=4.9, minimum_credit_cents=5) == 0
    assert _credit_target_contracts(credit_cents=5, minimum_credit_cents=5) == 1
    assert _credit_target_contracts(credit_cents=5.49, minimum_credit_cents=5) == 1
    assert _credit_target_contracts(credit_cents=5.5, minimum_credit_cents=5) == 2
    assert _credit_target_contracts(credit_cents=7, minimum_credit_cents=5) == 5


def test_v2_order_mapping(monkeypatch) -> None:
    from data import kalshi_trading

    requests = []

    def fake_request(method, path, *, params=None, body=None):
        requests.append((method, path, body))
        return {"order_id": "order-1", "fill_count": "2.00"}

    monkeypatch.setattr(kalshi_trading, "_live_order_entry_enabled", True)
    monkeypatch.setattr(kalshi_trading, "_request", fake_request)

    result = kalshi_trading.place_limit_order(
        market_ticker="TEST", side="no", action="buy", count=2, price_cents=40
    )
    body = requests[-1][2]
    assert requests[-1][:2] == ("POST", "/portfolio/events/orders")
    assert body["side"] == "ask"
    assert body["price"] == "0.6000"
    assert body["count"] == "2.00"
    assert body["time_in_force"] == "immediate_or_cancel"
    assert body["reduce_only"] is False
    assert result["order"]["order_id"] == "order-1"

    kalshi_trading.place_limit_order(
        market_ticker="TEST", side="yes", action="buy", count=1, price_cents=6.3
    )
    assert requests[-1][2]["price"] == "0.0630"

    kalshi_trading.place_limit_order(
        market_ticker="TEST", side="no", action="sell", count=1, price_cents=40
    )
    assert requests[-1][2]["side"] == "bid"
    assert requests[-1][2]["reduce_only"] is True

    monkeypatch.setattr(kalshi_trading, "_live_order_entry_enabled", False)
    with pytest.raises(RuntimeError, match="Start live trading"):
        kalshi_trading.place_limit_order(
            market_ticker="TEST", side="yes", action="buy", count=1, price_cents=40
        )
    kalshi_trading.place_limit_order(
        market_ticker="TEST",
        side="yes",
        action="sell",
        count=1,
        price_cents=40,
        allow_when_stopped=True,
    )
    assert requests[-1][2]["reduce_only"] is True


def test_fee_aware_signal_is_small_and_actionable() -> None:
    book = OrderBook("TEST")
    book.load_ws_snapshot(
        {
            "yes_dollars_fp": [[0.59, 200], [0.58, 100]],
            "no_dollars_fp": [[0.40, 200], [0.39, 100]],
            "seq": 1,
        }
    )
    settings = TradingSettings()
    signal, reason, diagnostics = build_trade_signal(
        pricing={
            "ready": True,
            "p_model": 0.68,
            "seconds_to_expiry": 850,
            "vol_is_fallback": False,
        },
        market_ticker="TEST",
        book=book,
        settings=settings,
        bankroll_cents=100_000,
        available_cash_cents=90_000,
        runtime_uptime_seconds=60,
    )
    assert reason == "ev_signal_ready"
    assert signal is not None
    assert signal.side == "yes"
    assert signal.count == min(
        diagnostics["credit_target_contracts"], diagnostics["kelly_target_contracts"]
    )
    assert signal.credit_cents == diagnostics["credit_cents"]
    assert signal.edge_cents >= settings.min_edge_cents


def test_trading_styles_control_strategy_orders() -> None:
    book = OrderBook("TEST")
    book.load_ws_snapshot(
        {"yes_dollars_fp": [[0.59, 10]], "no_dollars_fp": [[0.40, 10]]}
    )
    inputs = {
        "pricing": {
            "ready": True,
            "p_model": 0.10,
            "seconds_to_expiry": 850,
            "vol_is_fallback": False,
        },
        "market_ticker": "TEST",
        "book": book,
        "bankroll_cents": 100_000,
        "open_yes_contracts": 1,
        "open_yes_avg_entry_cents": 60,
        "runtime_uptime_seconds": 60,
    }

    signal, reason, _ = build_trade_signal(
        **inputs, settings=TradingSettings(trading_style="semi")
    )
    assert signal is not None and signal.action == "sell"
    assert reason == "stop_loss_guardrail_exit_yes"

    signal, reason, _ = build_trade_signal(
        **inputs, settings=TradingSettings(trading_style="click")
    )
    assert signal is None and reason == "click_trading"


def test_fee_rounding_matches_order_level_formula() -> None:
    assert taker_fee_cents_per_contract(50) == 2
    assert taker_fee_cents_per_contract(50, fee_multiplier=2) == 4
    assert expected_value_cents(p_win=0.60, price_cents=55) == 3


def test_tapered_slippage_moves_by_exchange_ticks() -> None:
    ranges = [
        {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
        {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
        {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
    ]
    assert slipped_price_cents(6.2, 1, "up", ranges) == 6.3
    assert slipped_price_cents(10.0, 1, "up", ranges) == 11.0

    book = OrderBook("SUBPENNY")
    book.load_ws_snapshot(
        {"yes_dollars_fp": [[0.062, 10]], "no_dollars_fp": [[0.937, 10]]}
    )
    assert book.get_best_prices()[:2] == (6.2, 6.3)


def test_daily_loss_guard_persists(monkeypatch, tmp_path) -> None:
    from engine.trading import runtime

    state_path = tmp_path / "risk.json"
    monkeypatch.setattr(runtime, "EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setattr(runtime, "_risk_day", lambda: "2026-08-09")
    monkeypatch.setattr(runtime, "_risk_states", {})

    first, _ = runtime._sync_daily_risk(10_000, 10, "paper")
    locked, newly_locked = runtime._sync_daily_risk(9_000, 10, "paper")
    assert first["locked"] is False
    assert locked["locked"] is True
    assert newly_locked is True
    expected_path = state_path.with_name(f"{state_path.name}.paper")
    assert expected_path.exists()


def test_disarmed_runtime_cannot_submit(monkeypatch) -> None:
    from engine.trading import runtime

    monkeypatch.setattr(runtime, "_armed", False)
    monkeypatch.setattr(
        runtime,
        "place_limit_order",
        lambda **_: (_ for _ in ()).throw(AssertionError("order submitted")),
    )
    status, result = runtime._submit_live_signal(object(), 1_000, 30)
    assert (status, result) == ("disarmed", None)


def test_start_selects_execution_mode_without_confirmation(monkeypatch) -> None:
    from engine.trading import runtime

    account = {"cash_cents": 10_000, "equity_cents": 10_000, "positions": []}
    monkeypatch.setattr(runtime, "_execution_mode", None)
    monkeypatch.setattr(runtime, "_armed", False)
    monkeypatch.setattr(runtime, "_runtime_state", {"armed": False})
    monkeypatch.setattr(runtime, "_fetch_account_snapshot", lambda mode: account)
    monkeypatch.setattr(
        runtime, "_sync_daily_risk", lambda *_: ({"locked": False}, False)
    )
    monkeypatch.setattr(runtime, "_emit_event", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runtime, "set_live_order_entry_enabled", lambda _enabled: None)

    result = runtime.control_trading("start", "paper")

    assert result == {"ok": True, "status": "started", "execution_mode": "paper"}
    assert runtime._get_execution_mode() == "paper"
    assert runtime._is_armed() is True


def test_paper_ioc_accounting_and_settlement() -> None:
    book = OrderBook("TEST")
    book.load_ws_snapshot(
        {"yes_dollars_fp": [[0.59, 10]], "no_dollars_fp": [[0.40, 2]]}
    )
    account = PaperAccount(10_000)

    buy = account.place_ioc(
        market_ticker="TEST",
        side="yes",
        action="buy",
        count=5,
        price_cents=61,
        book=book,
    )
    assert buy["order"]["fill_count"] == "2"
    assert account.snapshot()["equity_cents"] == 9_996

    sell = account.place_ioc(
        market_ticker="TEST",
        side="yes",
        action="sell",
        count=1,
        price_cents=58,
        book=book,
    )
    assert sell["order"]["fill_count"] == "1"
    assert account.settle("TEST", "no")["closed_contracts"] == 1
    assert account.snapshot()["cash_cents"] == 9_933


def test_paper_mode_cannot_reach_live_order_api(monkeypatch) -> None:
    from engine.trading import runtime

    book = OrderBook("TEST")
    book.load_ws_snapshot(
        {"yes_dollars_fp": [[0.59, 10]], "no_dollars_fp": [[0.40, 10]]}
    )
    monkeypatch.setattr(runtime, "_paper_account", PaperAccount(10_000))
    monkeypatch.setattr(runtime, "get_live_book", lambda: book)
    monkeypatch.setattr(
        runtime,
        "place_limit_order",
        lambda **_: (_ for _ in ()).throw(AssertionError("live API reached")),
    )

    result = runtime._place_order(
        execution_mode="paper",
        market_ticker="TEST",
        side="yes",
        action="buy",
        count=1,
        price_cents=61,
    )
    assert result["order"]["fill_count"] == "1"


def test_discretionary_order_uses_shared_risk_boundary(monkeypatch) -> None:
    from engine.trading import runtime

    book = OrderBook("TEST")
    book.load_ws_snapshot(
        {"yes_dollars_fp": [[0.59, 10]], "no_dollars_fp": [[0.40, 10]]}
    )
    monkeypatch.setattr(runtime, "_execution_mode", "paper")
    monkeypatch.setattr(runtime, "_armed", True)
    monkeypatch.setattr(runtime, "_paper_account", PaperAccount(10_000))
    monkeypatch.setattr(runtime, "get_live_book", lambda: book)
    monkeypatch.setattr(
        runtime,
        "get_live_market_info",
        lambda: {"ticker": "TEST", "price_ranges": []},
    )
    monkeypatch.setattr(
        runtime,
        "_sync_daily_risk",
        lambda *_: ({"locked": False}, False),
    )

    result = runtime.submit_manual_order(
        side="yes", action="buy", count=2
    )
    assert result["status"] == "manual_paper_filled"
    assert runtime._paper_account.snapshot()["positions"][0]["contracts"] == 2

    monkeypatch.setattr(runtime, "_armed", False)
    with pytest.raises(RuntimeError, match="Start trading"):
        runtime.submit_manual_order(
            side="yes", action="buy", count=1
        )
