"""
Order-book microstructure skew for a Kalshi YES contract: run on each orderbook update.

Computes:
  * **OBI** — order book imbalance from resting bid vs ask depth (top-N levels on the YES book).
  * **MPP** — mid-price drift: change in YES probability mid over a short lookback, spread-normalized.

Combined via a logistic sigmoid into ``P_book ∈ (0, 1)`` (higher = more upward pressure on YES fair).
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from engine.orderbook import OrderBook


def _sigmoid(x: float) -> float:
    if x >= 35.0:
        return 1.0 - 1e-15
    if x <= -35.0:
        return 1e-15
    return 1.0 / (1.0 + math.exp(-x))


def resting_obi(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    depth: int,
) -> float:
    """
    (V_bid - V_ask) / (V_bid + V_ask) on the YES book (prices in cents, qty = contracts).
    """
    if depth <= 0:
        return 0.0

    vb = sum(q for _, q in bids[:depth])
    va = sum(q for _, q in asks[:depth])
    tot = vb + va
    if tot <= 0:
        return 0.0
    return (vb - va) / tot


def yes_mid_and_spread_cents(
    yes_bids: list[tuple[float, float]],
    yes_asks: list[tuple[float, float]],
) -> tuple[float | None, float]:
    """YES mid in cents; spread in cents (at least 1e-6 to avoid div-by-zero)."""
    if not yes_bids or not yes_asks:
        return None, 1.0
    bb = float(yes_bids[0][0])
    ba = float(yes_asks[0][0])
    mid = 0.5 * (bb + ba)
    sp = max(ba - bb, 1e-6)
    return mid, sp


@dataclass
class BookMicrostructureState:
    """Stateful orderbook-pressure calculator."""

    obi_depth: int = 10
    mpp_window_sec: float = 45.0
    sigmoid_bias: float = 0.0
    w_obi: float = 2.0
    w_mpp: float = 1.25

    _mid_hist: deque[tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=512)
    )

    def _purge_mids(self, now: float) -> None:
        cutoff = now - max(self.mpp_window_sec * 3.0, self.mpp_window_sec + 1.0)
        while self._mid_hist and self._mid_hist[0][0] < cutoff:
            self._mid_hist.popleft()

    def mpp_drift_normalized(
        self, mid: float, now: float, spread_cents: float
    ) -> float:
        """
        Mid change vs first snapshot at or after ``now - mpp_window_sec``, normalized by spread.
        """
        self._purge_mids(now)
        target_t = now - self.mpp_window_sec
        old_mid: float | None = None
        for t, m in self._mid_hist:
            if t >= target_t:
                old_mid = m
                break
        if old_mid is None:
            old_mid = self._mid_hist[0][1] if self._mid_hist else mid
        self._mid_hist.append((now, mid))
        dm = mid - old_mid
        return dm / max(spread_cents, 1e-6)

    def compute(
        self,
        yes_bids: list[tuple[float, float]],
        yes_asks: list[tuple[float, float]],
        now: float | None = None,
    ) -> dict[str, Any]:
        """
        Returns feature dict including ``p_book`` and raw OBI / MPP.
        """
        t = time.time() if now is None else float(now)
        obi = resting_obi(yes_bids, yes_asks, self.obi_depth)
        mid, sp = yes_mid_and_spread_cents(yes_bids, yes_asks)
        if mid is None:
            mpp = 0.0
        else:
            mpp = self.mpp_drift_normalized(mid, t, sp)

        z = self.sigmoid_bias + self.w_obi * obi + self.w_mpp * mpp
        p_book = _sigmoid(z)

        return {
            "p_book": p_book,
            "obi": obi,
            "mpp": mpp,
            "yes_mid_cents": mid,
            "yes_spread_cents": sp,
            "z": z,
            "ts": t,
        }


_GLOBAL_MICRO = BookMicrostructureState()
_LAST_P_BOOK: dict[str, Any] | None = None


def on_live_orderbook_update(book: OrderBook) -> dict[str, Any] | None:
    """Recompute OBI / MPP / P_book after a successful local book apply."""
    global _LAST_P_BOOK
    if not book.initialized:
        return None
    yes_bids, yes_asks, _, _ = book.get_orderbook()
    _LAST_P_BOOK = _GLOBAL_MICRO.compute(yes_bids, yes_asks)
    return _LAST_P_BOOK


def get_last_p_book_snapshot() -> dict[str, Any] | None:
    return _LAST_P_BOOK


def reset_book_microstructure_for_new_market() -> None:
    """Clear mid history when the Kalshi stream switches to the next 15m contract."""
    global _GLOBAL_MICRO, _LAST_P_BOOK
    _GLOBAL_MICRO = BookMicrostructureState()
    _LAST_P_BOOK = None
