"""
Order-book microstructure skew for a Kalshi YES contract: run on each orderbook update.

Computes:
  * **OBI** — order book imbalance from resting bid vs ask depth (top-N levels on the YES book).
  * **TFI** — trade flow imbalance from recent aggressor-side prints (yes taker lifts ask → +weight).
  * **MPP** — mid-price drift: change in YES probability mid over a short lookback, spread-normalized.

Combined via a logistic sigmoid into ``P_book ∈ (0, 1)`` (higher = more upward pressure on YES fair).
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

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
class TradePrint:
    ts: float
    taker_side: Literal["yes", "no"]
    count: float


@dataclass
class BookMicrostructureState:
    """
    Stateful: append trades as they arrive; call :meth:`compute` on each orderbook update.
    """

    obi_depth: int = 10
    mpp_window_sec: float = 45.0
    trade_window_sec: float = 120.0
    sigmoid_bias: float = 0.0
    w_obi: float = 2.0
    w_tfi: float = 1.5
    w_mpp: float = 1.25

    _trades: deque[TradePrint] = field(default_factory=lambda: deque(maxlen=500))
    _mid_hist: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=512))

    def on_trade(self, taker_side: str, count: float, ts: float | None = None) -> None:
        """
        Record a public trade (Kalshi ``taker_side`` is ``\"yes\"`` or ``\"no\"``).

        Buying YES aggressively ⇒ ``taker_side == \"yes\"`` ⇒ positive flow for YES price.
        """
        side = (taker_side or "").lower()
        if side not in ("yes", "no"):
            return
        t = time.time() if ts is None else float(ts)
        self._trades.append(TradePrint(ts=t, taker_side=side, count=max(float(count), 0.0)))

    def _purge_trades(self, now: float) -> None:
        cutoff = now - self.trade_window_sec
        while self._trades and self._trades[0].ts < cutoff:
            self._trades.popleft()

    def _purge_mids(self, now: float) -> None:
        cutoff = now - max(self.mpp_window_sec * 3.0, self.mpp_window_sec + 1.0)
        while self._mid_hist and self._mid_hist[0][0] < cutoff:
            self._mid_hist.popleft()

    def trade_flow_imbalance(self, now: float) -> float:
        """
        (V_yes_taker - V_no_taker) / (V_yes_taker + V_no_taker) over ``trade_window_sec``.
        """
        self._purge_trades(now)
        vy = 0.0
        vn = 0.0
        for tr in self._trades:
            if tr.taker_side == "yes":
                vy += tr.count
            else:
                vn += tr.count
        tot = vy + vn
        if tot <= 0:
            return 0.0
        return (vy - vn) / tot

    def mpp_drift_normalized(self, mid: float, now: float, spread_cents: float) -> float:
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
        Returns feature dict including ``p_book`` and raw OBI / TFI / MPP.
        """
        t = time.time() if now is None else float(now)
        obi = resting_obi(yes_bids, yes_asks, self.obi_depth)
        tfi = self.trade_flow_imbalance(t)
        mid, sp = yes_mid_and_spread_cents(yes_bids, yes_asks)
        if mid is None:
            mpp = 0.0
        else:
            mpp = self.mpp_drift_normalized(mid, t, sp)

        z = self.sigmoid_bias + self.w_obi * obi + self.w_tfi * tfi + self.w_mpp * mpp
        p_book = _sigmoid(z)

        return {
            "p_book": p_book,
            "obi": obi,
            "tfi": tfi,
            "mpp": mpp,
            "yes_mid_cents": mid,
            "yes_spread_cents": sp,
            "z": z,
            "ts": t,
        }


def compute_p_book_from_orderbook(
    snapshot: dict[str, Any],
    state: BookMicrostructureState,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """
    Adapter for :func:`engine.streamer.get_live_orderbook_snapshot` payloads.
    """
    yes_bids = snapshot.get("yes_bids") or []
    yes_asks = snapshot.get("yes_asks") or []
    if not isinstance(yes_bids, list):
        yes_bids = []
    if not isinstance(yes_asks, list):
        yes_asks = []
    levels_b = [(float(p), float(q)) for p, q in yes_bids if isinstance(p, (int, float))]
    levels_a = [(float(p), float(q)) for p, q in yes_asks if isinstance(p, (int, float))]
    return state.compute(levels_b, levels_a, now=now)


_GLOBAL_MICRO = BookMicrostructureState()
_LAST_P_BOOK: dict[str, Any] | None = None


def on_live_orderbook_update(book: OrderBook) -> dict[str, Any] | None:
    """Recompute OBI / TFI / MPP / P_book after a successful local book apply."""
    global _LAST_P_BOOK
    if not book.initialized:
        return None
    yes_bids, yes_asks, _, _ = book.get_orderbook()
    _LAST_P_BOOK = _GLOBAL_MICRO.compute(yes_bids, yes_asks)
    return _LAST_P_BOOK


def on_public_trade(taker_side: str, count: float, ts: float | None = None) -> None:
    """Wire Kalshi public-trades WS (``taker_side``, ``count``) to feed TFI."""
    _GLOBAL_MICRO.on_trade(taker_side, count, ts=ts)


def get_last_p_book_snapshot() -> dict[str, Any] | None:
    return _LAST_P_BOOK


def get_book_microstructure_state() -> BookMicrostructureState:
    return _GLOBAL_MICRO


def reset_book_microstructure_for_new_market() -> None:
    """Clear mid/trade history when the Kalshi stream switches to the next 15m contract."""
    global _GLOBAL_MICRO, _LAST_P_BOOK
    _GLOBAL_MICRO = BookMicrostructureState()
    _LAST_P_BOOK = None
