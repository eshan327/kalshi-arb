from dataclasses import replace

from engine.orderbook import OrderBook
from engine.shadow.settings_state import get_shadow_settings_model
from engine.shadow.signal_engine import build_shadow_signal


def _build_book() -> OrderBook:
    book = OrderBook("TEST-TICKER")
    book.load_ws_snapshot(
        {
            "yes_dollars_fp": [[0.59, 200], [0.58, 110]],
            "no_dollars_fp": [[0.40, 180], [0.39, 90]],
            "seq": 101,
        }
    )
    return book


def test_signal_emits_when_edge_positive() -> None:
    settings = get_shadow_settings_model()
    book = _build_book()

    signal, reason, _ = build_shadow_signal(
        pricing={"ready": True, "p_model": 0.63, "seconds_to_expiry": 500},
        market_ticker="TEST-TICKER",
        book=book,
        settings=settings,
        bankroll_cents=100_000,
    )

    assert reason == "ev_signal_ready"
    assert signal is not None
    assert signal.action == "buy"
    assert signal.edge_cents >= settings.min_edge_cents
    assert signal.count <= 50


def test_signal_blocks_when_threshold_too_high() -> None:
    settings = replace(get_shadow_settings_model(), min_edge_cents=5.0)
    book = _build_book()

    signal, reason, _ = build_shadow_signal(
        pricing={"ready": True, "p_model": 0.63, "seconds_to_expiry": 500},
        market_ticker="TEST-TICKER",
        book=book,
        settings=settings,
        bankroll_cents=100_000,
    )

    assert signal is None
    assert reason == "edge_below_threshold"


def test_signal_blocks_outside_probability_guardrails() -> None:
    settings = get_shadow_settings_model()
    book = _build_book()

    signal, reason, _ = build_shadow_signal(
        pricing={"ready": True, "p_model": 0.12, "seconds_to_expiry": 500},
        market_ticker="TEST-TICKER",
        book=book,
        settings=settings,
        bankroll_cents=100_000,
    )

    assert signal is None
    assert reason == "model_probability_out_of_bounds"


def test_signal_emits_sell_clip_on_edge_reversal() -> None:
    settings = get_shadow_settings_model()
    book = OrderBook("TEST-TICKER")
    book.load_ws_snapshot(
        {
            "yes_dollars_fp": [[0.75, 200], [0.74, 100]],
            "no_dollars_fp": [[0.26, 180], [0.25, 120]],
            "seq": 202,
        }
    )

    signal, reason, _ = build_shadow_signal(
        pricing={"ready": True, "p_model": 0.55, "seconds_to_expiry": 500},
        market_ticker="TEST-TICKER",
        book=book,
        settings=settings,
        bankroll_cents=100_000,
        open_yes_contracts=130,
        open_no_contracts=0,
    )

    assert signal is not None
    assert signal.action == "sell"
    assert signal.side == "yes"
    assert signal.count == 50
    assert reason == "edge_reversal_exit_yes"
