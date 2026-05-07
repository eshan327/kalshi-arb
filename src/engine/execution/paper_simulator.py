from __future__ import annotations

import math
import random
import time

from core.config import (
    PAPER_SIM_AGGRESSIVE_FILL_PROB,
    PAPER_SIM_LATENCY_MS,
    PAPER_SIM_MAX_PARTIAL_FILL_FRACTION,
    PAPER_SIM_MIN_PARTIAL_FILL_FRACTION,
    PAPER_SIM_PASSIVE_BASE_FILL_PROB,
    PAPER_SIM_PRICE_DRIFT_CENTS_PER_SEC,
    PAPER_SIM_QUEUE_AHEAD_FRACTION,
    PAPER_SIM_QUEUE_DECAY,
    PAPER_SIM_RANDOM_SEED,
)
from engine.execution.paper_models import PaperOrder, SimulatedFillDecision
from engine.orderbook import OrderBook


def _clip_probability(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class PaperFillSimulator:
    """Hybrid fill model: aggressive crosses fill quickly, passive orders fill probabilistically."""

    def __init__(self) -> None:
        self._aggressive_fill_prob = _clip_probability(PAPER_SIM_AGGRESSIVE_FILL_PROB)
        self._passive_base_fill_prob = _clip_probability(PAPER_SIM_PASSIVE_BASE_FILL_PROB)
        self._queue_decay = max(0.0, float(PAPER_SIM_QUEUE_DECAY))
        self._queue_ahead_fraction = max(0.0, float(PAPER_SIM_QUEUE_AHEAD_FRACTION))
        self._min_partial_fraction = max(0.05, min(1.0, float(PAPER_SIM_MIN_PARTIAL_FILL_FRACTION)))
        self._max_partial_fraction = max(
            self._min_partial_fraction,
            min(1.0, float(PAPER_SIM_MAX_PARTIAL_FILL_FRACTION)),
        )
        self._latency_ms = max(0.0, float(PAPER_SIM_LATENCY_MS))
        self._drift_cents_per_sec = float(PAPER_SIM_PRICE_DRIFT_CENTS_PER_SEC)
        self._rng = random.Random(int(PAPER_SIM_RANDOM_SEED))

    @staticmethod
    def _price_levels_for_side(book: OrderBook, side: str) -> tuple[dict[int, float], int | None, int | None]:
        yes_bids, yes_asks, no_bids, no_asks = book.get_orderbook_top_n(80)
        if side == "yes":
            bids = {int(round(float(price))): float(qty) for price, qty in yes_bids}
            best_bid = int(round(float(yes_bids[0][0]))) if yes_bids else None
            best_ask = int(round(float(yes_asks[0][0]))) if yes_asks else None
        else:
            bids = {int(round(float(price))): float(qty) for price, qty in no_bids}
            best_bid = int(round(float(no_bids[0][0]))) if no_bids else None
            best_ask = int(round(float(no_asks[0][0]))) if no_asks else None
        return bids, best_bid, best_ask

    def _aggressive_fill(
        self,
        *,
        order: PaperOrder,
        best_bid: int,
        best_ask: int,
        spread_cents: float,
        now_ts: float,
    ) -> SimulatedFillDecision:
        thin_book_penalty = min(0.35, max(0.0, (spread_cents - 1.0) * 0.07))
        fill_probability = _clip_probability(self._aggressive_fill_prob - thin_book_penalty)

        if self._rng.random() > fill_probability:
            return SimulatedFillDecision(
                would_fill=False,
                fill_count=0,
                fill_price_cents=None,
                is_taker=True,
                reason="aggressive_rejected",
                fill_probability=fill_probability,
                ts=now_ts,
                best_bid_cents=best_bid,
                best_ask_cents=best_ask,
                spread_cents=spread_cents,
            )

        drift = (self._latency_ms / 1000.0) * self._drift_cents_per_sec
        fill_price = int(round(best_ask + max(0.0, drift)))
        fill_price = max(1, min(order.price_cents, min(99, fill_price)))

        return SimulatedFillDecision(
            would_fill=True,
            fill_count=order.remaining_count,
            fill_price_cents=fill_price,
            is_taker=True,
            reason="crossed_spread",
            fill_probability=fill_probability,
            ts=now_ts,
            best_bid_cents=best_bid,
            best_ask_cents=best_ask,
            spread_cents=spread_cents,
        )

    def _passive_fill(
        self,
        *,
        order: PaperOrder,
        bids: dict[int, float],
        best_bid: int,
        best_ask: int,
        p_book_quality: float | None,
        now_ts: float,
    ) -> SimulatedFillDecision:
        queue_here = max(0.0, float(bids.get(order.price_cents, 0.0)))
        queue_ahead = queue_here * self._queue_ahead_fraction

        edge_to_ask = max(0, best_ask - order.price_cents)
        price_proximity = 1.0 / (1.0 + float(edge_to_ask))
        queue_penalty = math.exp(-self._queue_decay * queue_ahead / max(1.0, float(order.remaining_count)))
        fill_probability = self._passive_base_fill_prob * queue_penalty * (0.55 + 0.45 * price_proximity)

        if isinstance(p_book_quality, (int, float)):
            q = max(0.0, min(1.0, float(p_book_quality)))
            fill_probability *= 0.60 + 0.60 * q
        fill_probability = _clip_probability(fill_probability)

        if self._rng.random() > fill_probability:
            return SimulatedFillDecision(
                would_fill=False,
                fill_count=0,
                fill_price_cents=None,
                is_taker=False,
                reason="passive_not_filled",
                fill_probability=fill_probability,
                ts=now_ts,
                best_bid_cents=best_bid,
                best_ask_cents=best_ask,
                spread_cents=float(best_ask - best_bid),
            )

        max_qty = order.remaining_count
        if order.price_cents >= best_bid:
            partial_fraction = self._min_partial_fraction + (
                self._max_partial_fraction - self._min_partial_fraction
            ) * self._rng.random()
        else:
            partial_fraction = self._min_partial_fraction * (0.6 + 0.4 * self._rng.random())

        fill_count = max(1, min(max_qty, int(round(max_qty * partial_fraction))))

        return SimulatedFillDecision(
            would_fill=True,
            fill_count=fill_count,
            fill_price_cents=order.price_cents,
            is_taker=False,
            reason="passive_queue_fill",
            fill_probability=fill_probability,
            ts=now_ts,
            best_bid_cents=best_bid,
            best_ask_cents=best_ask,
            spread_cents=float(best_ask - best_bid),
        )

    def simulate(
        self,
        *,
        order: PaperOrder,
        book: OrderBook | None,
        p_book_quality: float | None,
        now_ts: float | None = None,
    ) -> SimulatedFillDecision:
        ts = time.time() if now_ts is None else float(now_ts)

        if book is None or not book.initialized:
            return SimulatedFillDecision(
                would_fill=False,
                fill_count=0,
                fill_price_cents=None,
                is_taker=False,
                reason="no_orderbook",
                fill_probability=0.0,
                ts=ts,
                best_bid_cents=None,
                best_ask_cents=None,
                spread_cents=None,
            )

        bids, best_bid, best_ask = self._price_levels_for_side(book, order.side)
        if best_bid is None or best_ask is None:
            return SimulatedFillDecision(
                would_fill=False,
                fill_count=0,
                fill_price_cents=None,
                is_taker=False,
                reason="missing_best_quotes",
                fill_probability=0.0,
                ts=ts,
                best_bid_cents=best_bid,
                best_ask_cents=best_ask,
                spread_cents=None,
            )

        spread_cents = max(1.0, float(best_ask - best_bid))
        if order.execution_intent == "taker" or order.price_cents >= best_ask:
            return self._aggressive_fill(
                order=order,
                best_bid=best_bid,
                best_ask=best_ask,
                spread_cents=spread_cents,
                now_ts=ts,
            )

        return self._passive_fill(
            order=order,
            bids=bids,
            best_bid=best_bid,
            best_ask=best_ask,
            p_book_quality=p_book_quality,
            now_ts=ts,
        )
