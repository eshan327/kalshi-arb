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
    assert signal.edge_cents >= settings.min_edge_cents


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
