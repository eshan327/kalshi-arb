from __future__ import annotations

import pytest

from engine.trading.paper import PaperAccount
from engine.trading.settings import TradingSettings
from engine.trading.strategy import build_trade_signal
from research.strategy_backtest import DailyRiskTracker, _record_fill, candle_book


def test_historical_candle_book_preserves_live_yes_no_quote_mapping():
    candle = {
        "yes_bid": {"close": "0.5900"},
        "yes_ask": {"close": "0.6000"},
    }
    book = candle_book("TEST", candle, eval_ts=1000.0, assumed_top_size=10)
    assert book is not None
    assert book.get_best_prices() == (59.0, 60.0, 40.0, 41.0)
    assert book.get_orderbook_top_n(1)[1][0][1] == 10
    assert book.last_verified_ts == 1000.0


def test_strategy_replay_uses_production_limit_but_fills_at_observed_taker_quote():
    candle = {
        "yes_bid": {"close": "0.5900"},
        "yes_ask": {"close": "0.6000"},
    }
    book = candle_book("TEST", candle, eval_ts=1000.0, assumed_top_size=10)
    assert book is not None

    signal, reason, _ = build_trade_signal(
        pricing={
            "ready": True,
            "p_model": 0.68,
            "seconds_to_expiry": 800,
            "vol_is_fallback": False,
        },
        market_ticker="TEST",
        book=book,
        settings=TradingSettings(),
        bankroll_cents=100_000,
        available_cash_cents=97_500,
        fee_multiplier=1.0,
        fee_type="quadratic",
        now_ts=1000.0,
    )
    assert reason == "ev_signal_ready"
    assert signal is not None and signal.action == "buy" and signal.side == "yes"
    assert signal.quote_price_cents >= 60.0

    account = PaperAccount(100_000)
    fill = _record_fill(account, signal=signal, book=book, fee_multiplier=1.0)
    assert fill is not None
    assert fill.fill_price_cents == 60.0
    assert fill.filled_count == signal.count
    assert fill.fees_cents > 0
    # The hypothetical fill consumes the assumed archived top size instead of
    # letting later same-timestamp orders reuse synthetic liquidity.
    assert book.get_best_prices()[1] is None


def test_assumed_depth_caps_production_strategy_clip():
    candle = {
        "yes_bid": {"close": "0.5900"},
        "yes_ask": {"close": "0.6000"},
    }
    book = candle_book("TEST", candle, eval_ts=1000.0, assumed_top_size=2)
    assert book is not None

    signal, reason, diagnostics = build_trade_signal(
        pricing={
            "ready": True,
            "p_model": 0.90,
            "seconds_to_expiry": 800,
            "vol_is_fallback": False,
        },
        market_ticker="TEST",
        book=book,
        settings=TradingSettings(),
        bankroll_cents=100_000,
        available_cash_cents=97_500,
        fee_multiplier=1.0,
        fee_type="quadratic",
        now_ts=1000.0,
    )
    assert reason == "ev_signal_ready"
    assert signal is not None
    assert diagnostics["max_by_top_of_book"] == 2
    assert signal.count <= 2


def test_daily_risk_replay_locks_and_resets_on_new_york_day():
    risk = DailyRiskTracker()
    # Noon New York on consecutive September dates.
    day_one = 1789920000.0
    locked, new = risk.sync(ts=day_one, equity_cents=100_000, max_daily_loss_usd=10)
    assert (locked, new) == (False, False)

    locked, new = risk.sync(ts=day_one + 60, equity_cents=98_999, max_daily_loss_usd=10)
    assert (locked, new) == (True, True)

    locked, new = risk.sync(ts=day_one + 120, equity_cents=100_500, max_daily_loss_usd=10)
    assert (locked, new) == (True, False)

    locked, new = risk.sync(ts=day_one + 86_400, equity_cents=100_500, max_daily_loss_usd=10)
    assert (locked, new) == (False, False)



def test_strategy_backtest_preserves_full_settings_when_no_history(monkeypatch):
    import research.strategy_backtest as replay

    monkeypatch.setattr(replay, "get_settled_markets", lambda _series: [])
    settings = TradingSettings(
        min_edge_cents=3.0,
        deterministic_min_edge_cents=0.75,
        kelly_fraction=0.10,
        max_position_fraction=0.03,
        max_order_contracts=4,
        max_position_usd=25.0,
        max_daily_loss_usd=7.0,
        cash_buffer_usd=12.0,
        cooldown_seconds=9,
        slippage_ticks=2,
        volatility_scale=1.25,
    )
    _, _, _, summary = replay.run_strategy_backtest(
        asset="BTC",
        max_markets=10,
        starting_cash_cents=100_000,
        fee_multiplier=1.0,
        assumed_top_size=10,
        settings=settings,
    )
    assert summary["settings"] == settings.model_dump()
    assert summary["cf_spot_resolution"] == "unavailable"



def test_strategy_candle_book_accepts_live_tier_dollar_fields():
    candle = {
        "yes_bid": {"close_dollars": "0.5900"},
        "yes_ask": {"close_dollars": "0.6000"},
    }
    book = candle_book("TEST", candle, eval_ts=1000.0, assumed_top_size=10)
    assert book is not None
    assert book.get_best_prices() == (59.0, 60.0, 40.0, 41.0)
