import asyncio
import json

import pytest

from engine.orderbook import OrderBook
from engine.trading.fees import (
    expected_value_cents,
    taker_fee_cents_per_contract,
)
from engine.trading.paper import PaperAccount
from engine.trading.settings import TradingSettings
from engine.trading.strategy import (
    _kelly_target_contracts,
    build_trade_signal,
    slipped_price_cents,
)


def test_orderbook_quotes_and_market_midpoint_use_yes_probability() -> None:
    from engine.trading.runtime import _monologue

    book = OrderBook("TEST")
    book.load_rest_snapshot(
        {
            "yes_dollars_fp": [["0.2500", "10.00"]],
            "no_dollars_fp": [["0.7200", "20.00"]],
        }
    )
    assert book.get_best_prices() == (25.0, 28.0, 72.0, 75.0)

    message = _monologue(
        None,
        "at_target_allocation",
        {"p_model": 0.216},
        {"yes_bid_cents": 25.0, "yes_ask_cents": 28.0},
    )
    assert message["market_implied_probability"] == pytest.approx(0.265)
    assert message["action_intent"] == "PASS — target allocation reached"


def test_unified_websocket_no_price_is_converted_to_no_leg() -> None:
    book = OrderBook("TEST")
    book.load_rest_snapshot(
        {
            "yes_dollars_fp": [["0.4900", "10.00"]],
            "no_dollars_fp": [["0.5000", "20.00"]],
        }
    )

    book.apply_delta({"side": "no", "price_dollars": "0.6200", "delta_fp": "5.00"})

    assert book.get_best_prices() == (49.0, 50.0, 50.0, 51.0)
    assert book.no[38.0] == 5.0


def test_unified_websocket_snapshot_is_sequence_aligned_and_reciprocal() -> None:
    book = OrderBook("TEST")
    book.load_ws_snapshot(
        {
            "yes_dollars_fp": [["0.5500", "10.00"]],
            "no_dollars_fp": [["0.5700", "20.00"]],
        },
        seq=41,
    )

    assert book.expected_seq == 42
    assert book.get_best_prices() == (55.0, 57.0, 43.0, 45.0)


def test_sequence_gap_recovers_with_in_band_snapshot(monkeypatch) -> None:
    from engine import streamer

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages = iter(
                [
                    {
                        "type": "subscribed",
                        "msg": {"channel": "orderbook_delta", "sid": 9},
                    },
                    {
                        "type": "orderbook_snapshot",
                        "seq": 10,
                        "msg": {
                            "yes_dollars_fp": [["0.4900", "10.00"]],
                            "no_dollars_fp": [["0.5000", "20.00"]],
                        },
                    },
                    {
                        "type": "orderbook_delta",
                        "seq": 12,
                        "msg": {
                            "side": "yes",
                            "price_dollars": "0.4800",
                            "delta_fp": "1.00",
                        },
                    },
                    {
                        "type": "orderbook_snapshot",
                        "seq": 12,
                        "msg": {
                            "yes_dollars_fp": [["0.4800", "11.00"]],
                            "no_dollars_fp": [["0.5100", "20.00"]],
                        },
                    },
                    {"type": "noop", "msg": {}},
                ]
            )
            self.sent = []
            self.closed = False

        async def recv(self):
            return json.dumps(next(self.messages))

        async def send(self, message):
            self.sent.append(json.loads(message))

        async def close(self):
            self.closed = True

    ws = FakeWebSocket()
    close_checks = iter([False, False, False, False, False, True])
    monkeypatch.setattr(
        streamer,
        "connect_and_subscribe",
        lambda _ticker: asyncio.sleep(0, result=ws),
    )
    monkeypatch.setattr(streamer, "is_market_closed", lambda _ts: next(close_checks))
    monkeypatch.setattr(streamer, "on_live_orderbook_update", lambda _book: None)
    book = OrderBook("TEST")

    asyncio.run(streamer._stream_with_sync("TEST", book, market_close_ts=1))

    assert ws.sent[0]["params"] == {
        "sids": [9],
        "market_tickers": ["TEST"],
        "action": "get_snapshot",
    }
    assert book.expected_seq == 13
    assert book.get_best_prices() == (48.0, 51.0, 49.0, 52.0)
    assert ws.closed is True


def test_crossed_book_fails_closed() -> None:
    book = OrderBook("TEST")
    book.load_rest_snapshot(
        {
            "yes_dollars_fp": [["0.6100", "10.00"]],
            "no_dollars_fp": [["0.5400", "20.00"]],
        }
    )

    assert book.get_best_prices() == (None, None, None, None)


def test_kelly_target_is_bounded_by_bankroll_risk() -> None:
    high_target, _, high_cost, high_risk = _kelly_target_contracts(
        p_win=1.0,
        quote_price_cents=98,
        bankroll_cents=100_000,
        max_position_usd=50,
        max_position_fraction=0.05,
        kelly_scale=0.25,
        fee_cents=1,
    )
    longshot_target, _, longshot_cost, longshot_risk = _kelly_target_contracts(
        p_win=0.15,
        quote_price_cents=8,
        bankroll_cents=100_000,
        max_position_usd=50,
        max_position_fraction=0.05,
        kelly_scale=0.25,
        fee_cents=1,
    )

    assert high_target == int(high_risk // high_cost) == 50
    assert longshot_target == int(longshot_risk // longshot_cost)
    assert high_risk == 5_000
    assert longshot_risk < high_risk


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
    book.load_rest_snapshot(
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
        runtime_uptime_seconds=0,
    )
    assert reason == "ev_signal_ready"
    assert signal is not None
    assert signal.side == "yes"
    assert signal.count == min(
        settings.max_order_contracts,
        diagnostics["kelly_target_contracts"],
        diagnostics["max_by_top_of_book"],
    )
    assert signal.credit_cents == diagnostics["credit_cents"]
    assert signal.edge_cents >= settings.min_edge_cents
    assert diagnostics["position_notional_cap_cents"] <= 5_000


def test_locked_outcome_can_trade_near_expiry_at_smaller_edge() -> None:
    book = OrderBook("TEST")
    book.load_rest_snapshot(
        {"yes_dollars_fp": [[0.97, 100]], "no_dollars_fp": [[0.02, 100]]}
    )

    signal, reason, diagnostics = build_trade_signal(
        pricing={
            "ready": True,
            "p_model": 1 - 1e-12,
            "seconds_to_expiry": 10,
            "vol_is_fallback": False,
            "regime": "collapsed",
            "pricer_detail": {"required_future_avg": -1.0},
        },
        market_ticker="TEST",
        book=book,
        settings=TradingSettings(),
        bankroll_cents=100_000,
        available_cash_cents=90_000,
    )

    assert reason == "ev_signal_ready"
    assert signal is not None and signal.quote_price_cents == 98
    assert signal.edge_cents == pytest.approx(1)
    assert diagnostics["deterministic_outcome"] is True
    assert diagnostics["required_taker_edge_cents"] == pytest.approx(0.5)


def test_extreme_market_probability_is_sized_instead_of_blanket_blocked() -> None:
    inside_book = OrderBook("TEST")
    inside_book.load_rest_snapshot(
        {"yes_dollars_fp": [[0.50, 10]], "no_dollars_fp": [[0.49, 10]]}
    )
    pricing = {
        "ready": True,
        "p_model": 0.99,
        "seconds_to_expiry": 850,
        "vol_is_fallback": False,
    }
    _, inside_reason, inside_diagnostics = build_trade_signal(
        pricing=pricing,
        market_ticker="TEST",
        book=inside_book,
        settings=TradingSettings(),
        bankroll_cents=100_000,
        runtime_uptime_seconds=60,
    )

    assert inside_reason != "market_probability_out_of_bounds"
    assert inside_diagnostics["market_probability"] == pytest.approx(0.505)

    outside_book = OrderBook("TEST")
    outside_book.load_rest_snapshot(
        {"yes_dollars_fp": [[0.04, 10]], "no_dollars_fp": [[0.95, 10]]}
    )
    _, outside_reason, outside_diagnostics = build_trade_signal(
        pricing={**pricing, "p_model": 0.50},
        market_ticker="TEST",
        book=outside_book,
        settings=TradingSettings(),
        bankroll_cents=100_000,
        runtime_uptime_seconds=60,
    )

    assert outside_reason != "market_probability_out_of_bounds"
    assert outside_diagnostics["market_probability"] == pytest.approx(0.045)

    deterministic_book = OrderBook("TEST")
    deterministic_book.load_rest_snapshot(
        {"yes_dollars_fp": [[0.97, 10]], "no_dollars_fp": [[0.02, 10]]}
    )
    _, deterministic_reason, deterministic_diagnostics = build_trade_signal(
        pricing={
            **pricing,
            "regime": "collapsed",
            "pricer_detail": {"required_future_avg": -1.0},
        },
        market_ticker="TEST",
        book=deterministic_book,
        settings=TradingSettings(),
        bankroll_cents=100_000,
        runtime_uptime_seconds=60,
    )

    assert deterministic_reason != "market_probability_out_of_bounds"
    assert deterministic_diagnostics["deterministic_outcome"] is True


def test_systematic_strategy_manages_open_positions() -> None:
    book = OrderBook("TEST")
    book.load_rest_snapshot(
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

    signal, reason, _ = build_trade_signal(**inputs, settings=TradingSettings())
    assert signal is not None and signal.action == "sell"
    assert reason == "edge_reversal_exit_yes"
    assert signal.edge_cents > 0


def test_fee_rounding_matches_order_level_formula() -> None:
    assert taker_fee_cents_per_contract(50) == 2
    assert taker_fee_cents_per_contract(50, fee_multiplier=2) == 4
    assert expected_value_cents(p_win=0.60, price_cents=55) == 3
    assert taker_fee_cents_per_contract(6.3, action="buy") == pytest.approx(0.7)
    assert taker_fee_cents_per_contract(6.3, action="sell") == pytest.approx(1.3)


def test_tapered_slippage_moves_by_exchange_ticks() -> None:
    ranges = [
        {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
        {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
        {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
    ]
    assert slipped_price_cents(6.2, 1, "up", ranges) == 6.3
    assert slipped_price_cents(10.0, 1, "up", ranges) == 11.0

    book = OrderBook("SUBPENNY")
    book.load_rest_snapshot(
        {"yes_dollars_fp": [[0.062, 10]], "no_dollars_fp": [[0.937, 10]]}
    )
    assert book.get_best_prices()[:2] == (6.2, 6.3)


def test_daily_loss_guard_persists(monkeypatch, tmp_path) -> None:
    from engine.trading import runtime

    state_path = tmp_path / "risk.json"
    monkeypatch.setattr(runtime, "EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setattr(runtime, "_risk_day", lambda: "2026-08-09")
    monkeypatch.setattr(runtime, "_risk_states", {})
    monkeypatch.setattr(runtime, "_session_start_equities", {})

    first, _ = runtime._sync_daily_risk(10_000, 10, "paper", reset_session=True)
    locked, newly_locked = runtime._sync_daily_risk(9_000, 10, "paper")
    assert first["locked"] is False
    assert first["session_pnl_cents"] == 0
    assert locked["session_pnl_cents"] == -1_000
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
        runtime, "_sync_daily_risk", lambda *_, **__: ({"locked": False}, False)
    )
    monkeypatch.setattr(runtime, "_emit_event", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runtime, "set_live_order_entry_enabled", lambda _enabled: None)

    result = runtime.control_trading("start", "paper")

    assert result == {"ok": True, "status": "started", "execution_mode": "paper"}
    assert runtime._get_execution_mode() == "paper"
    assert runtime._is_armed() is True


def test_paper_ioc_accounting_and_settlement() -> None:
    book = OrderBook("TEST")
    book.load_rest_snapshot(
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


def test_paper_rollover_resolves_the_finished_market(monkeypatch) -> None:
    from engine.trading import runtime

    old_book = OrderBook("OLD")
    old_book.load_rest_snapshot(
        {"yes_dollars_fp": [[0.59, 10]], "no_dollars_fp": [[0.40, 10]]}
    )
    account = PaperAccount(10_000)
    account.place_ioc(
        market_ticker="OLD",
        side="yes",
        action="buy",
        count=1,
        price_cents=61,
        book=old_book,
    )
    events = []
    monkeypatch.setattr(runtime, "_paper_account", account)
    monkeypatch.setattr(runtime, "get_live_book", lambda: None)
    monkeypatch.setattr(
        runtime,
        "get_market",
        lambda ticker: {"ticker": ticker, "status": "settled", "result": "yes"},
    )
    monkeypatch.setattr(
        runtime,
        "_emit_event",
        lambda event_type, message, **detail: events.append(
            (event_type, message, detail)
        ),
    )

    asyncio.run(runtime._refresh_paper_account("NEW", "paper"))

    assert account.snapshot()["positions"] == []
    assert events[0][:2] == ("settlement", "paper_market_settled")


def test_subpenny_fees_and_pnl_follow_cash_rounding() -> None:
    book = OrderBook("TEST")
    book.load_rest_snapshot(
        {"yes_dollars_fp": [[0.05, 10]], "no_dollars_fp": [[0.937, 10]]}
    )
    account = PaperAccount(10_000)

    account.place_ioc(
        market_ticker="TEST",
        side="yes",
        action="buy",
        count=1,
        price_cents=7.3,
        book=book,
    )
    account.place_ioc(
        market_ticker="TEST",
        side="yes",
        action="sell",
        count=1,
        price_cents=4,
        book=book,
    )

    snapshot = account.snapshot()
    assert snapshot["cash_cents"] == 9_997
    assert snapshot["equity_cents"] == 9_997
    assert snapshot["realized_pnl_cents"] == pytest.approx(-3)


def test_live_portfolio_value_is_position_value(monkeypatch) -> None:
    from engine.trading import runtime

    monkeypatch.setattr(
        runtime,
        "get_balance_summary",
        lambda: {"balance": 40_000, "portfolio_value": 2_500, "updated_ts": 1},
    )
    monkeypatch.setattr(runtime, "get_positions", lambda: [])

    snapshot = runtime._fetch_account_snapshot("live")

    assert snapshot["equity_cents"] == 42_500
    assert snapshot["portfolio_value_cents"] == 2_500


def test_paper_mode_cannot_reach_live_order_api(monkeypatch) -> None:
    from engine.trading import runtime

    book = OrderBook("TEST")
    book.load_rest_snapshot(
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
    book.load_rest_snapshot(
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

    result = runtime.submit_manual_order(side="yes", action="buy", count=2)
    assert result["status"] == "manual_paper_filled"
    assert runtime._paper_account.snapshot()["positions"][0]["contracts"] == 2

    monkeypatch.setattr(runtime, "_armed", False)
    with pytest.raises(RuntimeError, match="Start trading"):
        runtime.submit_manual_order(side="yes", action="buy", count=1)
