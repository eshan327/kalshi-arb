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
from typing import Any, Literal, cast

from core.config import (
    P_BOOK_EMIT_INTERVAL_SEC,
    P_BOOK_FEATURE_EMA_ALPHA,
    P_BOOK_MPP_CLIP,
    P_BOOK_MPP_WINDOW_SEC,
    P_BOOK_MIN_SPREAD_CENTS,
    P_BOOK_MIN_TRADE_COUNT_FOR_QUALITY,
    P_BOOK_OBI_CLIP,
    P_BOOK_OBI_DEPTH,
    P_BOOK_PROB_EMA_ALPHA,
    P_BOOK_TFI_CLIP,
    P_BOOK_TRADE_WINDOW_SEC,
    P_BOOK_Z_CLIP,
)
from engine.orderbook import OrderBook


def _sigmoid(x: float) -> float:
    if x >= 35.0:
        return 1.0 - 1e-15
    if x <= -35.0:
        return 1e-15
    return 1.0 / (1.0 + math.exp(-x))


def _clip(value: float, bound: float) -> float:
    b = max(1e-9, float(bound))
    return max(-b, min(b, float(value)))


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

    obi_depth: int = P_BOOK_OBI_DEPTH
    mpp_window_sec: float = P_BOOK_MPP_WINDOW_SEC
    trade_window_sec: float = P_BOOK_TRADE_WINDOW_SEC
    sigmoid_bias: float = 0.0
    w_obi: float = 2.0
    w_tfi: float = 1.5
    w_mpp: float = 1.25
    min_spread_cents: float = P_BOOK_MIN_SPREAD_CENTS
    emit_interval_sec: float = P_BOOK_EMIT_INTERVAL_SEC
    feature_ema_alpha: float = P_BOOK_FEATURE_EMA_ALPHA
    prob_ema_alpha: float = P_BOOK_PROB_EMA_ALPHA
    obi_clip: float = P_BOOK_OBI_CLIP
    tfi_clip: float = P_BOOK_TFI_CLIP
    mpp_clip: float = P_BOOK_MPP_CLIP
    z_clip: float = P_BOOK_Z_CLIP
    min_trade_count_for_quality: int = P_BOOK_MIN_TRADE_COUNT_FOR_QUALITY

    _trades: deque[TradePrint] = field(default_factory=lambda: deque(maxlen=500))
    _mid_hist: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=512))
    _ema_obi: float | None = None
    _ema_tfi: float | None = None
    _ema_mpp: float | None = None
    _ema_p_book: float | None = None
    _last_emit_ts: float | None = None
    _updates: int = 0

    def on_trade(self, taker_side: str, count: float, ts: float | None = None) -> None:
        """
        Record a public trade (Kalshi ``taker_side`` is ``\"yes\"`` or ``\"no\"``).

        Buying YES aggressively ⇒ ``taker_side == \"yes\"`` ⇒ positive flow for YES price.
        """
        side = (taker_side or "").lower()
        if side not in ("yes", "no"):
            return
        t = time.time() if ts is None else float(ts)
        side_literal = cast(Literal["yes", "no"], side)
        self._trades.append(TradePrint(ts=t, taker_side=side_literal, count=max(float(count), 0.0)))

    def _purge_trades(self, now: float) -> None:
        cutoff = now - self.trade_window_sec
        while self._trades and self._trades[0].ts < cutoff:
            self._trades.popleft()

    def _purge_mids(self, now: float) -> None:
        cutoff = now - max(self.mpp_window_sec * 3.0, self.mpp_window_sec + 1.0)
        while self._mid_hist and self._mid_hist[0][0] < cutoff:
            self._mid_hist.popleft()

    def trade_flow_imbalance(self, now: float) -> tuple[float, int, float]:
        """
        (V_yes_taker - V_no_taker) / (V_yes_taker + V_no_taker) over ``trade_window_sec``.
        """
        self._purge_trades(now)
        vy = 0.0
        vn = 0.0
        count = 0
        for tr in self._trades:
            if tr.taker_side == "yes":
                vy += tr.count
            else:
                vn += tr.count
            count += 1
        tot = vy + vn
        if tot <= 0:
            return 0.0, count, 0.0
        return (vy - vn) / tot, count, tot

    def mpp_drift_normalized(self, mid: float, now: float, spread_cents: float) -> float:
        """
        Mid change vs rolling anchor before ``now - mpp_window_sec``, normalized by spread.
        """
        self._purge_mids(now)

        if not self._mid_hist:
            self._mid_hist.append((now, mid))
            return 0.0

        target_t = now - self.mpp_window_sec
        old_mid = self._mid_hist[0][1]
        for t, m in self._mid_hist:
            if t <= target_t:
                old_mid = m
            else:
                break

        self._mid_hist.append((now, mid))
        dm = mid - old_mid
        return dm / max(spread_cents, 1e-6)

    def _ema(self, old: float | None, new: float, alpha: float) -> float:
        a = max(0.01, min(1.0, float(alpha)))
        if old is None:
            return float(new)
        return old + a * (float(new) - old)

    def _quality_score(
        self,
        *,
        spread_cents: float,
        depth_total: float,
        trade_count: int,
        p_book_raw: float,
        p_book_smoothed: float,
        now: float,
    ) -> float:
        spread = max(1e-6, float(spread_cents))
        spread_quality = min(1.0, self.min_spread_cents / spread)
        depth_quality = min(1.0, max(0.0, depth_total / 45.0))
        trade_quality = min(
            1.0,
            max(0.0, float(trade_count) / max(1.0, float(self.min_trade_count_for_quality))),
        )

        hist_span = 0.0
        if self._mid_hist:
            hist_span = max(0.0, now - self._mid_hist[0][0])
        history_quality = min(1.0, hist_span / max(1.0, self.mpp_window_sec * 0.50))

        jitter = abs(float(p_book_raw) - float(p_book_smoothed))
        smoothness_quality = max(0.0, 1.0 - min(1.0, jitter / 0.20))

        return round(
            0.28 * spread_quality
            + 0.24 * depth_quality
            + 0.22 * trade_quality
            + 0.16 * history_quality
            + 0.10 * smoothness_quality,
            6,
        )

    def should_emit(self, now: float) -> bool:
        if self._last_emit_ts is None:
            return True
        return (now - self._last_emit_ts) >= self.emit_interval_sec

    def mark_emitted(self, now: float) -> None:
        self._last_emit_ts = float(now)

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
        obi_raw = _clip(resting_obi(yes_bids, yes_asks, self.obi_depth), self.obi_clip)
        tfi_raw, trade_count, trade_volume = self.trade_flow_imbalance(t)
        tfi_raw = _clip(tfi_raw, self.tfi_clip)

        mid, sp = yes_mid_and_spread_cents(yes_bids, yes_asks)
        if mid is None:
            mpp_raw = 0.0
            spread_for_math = max(1.0, self.min_spread_cents)
        else:
            spread_for_math = max(float(sp), self.min_spread_cents)
            mpp_raw = self.mpp_drift_normalized(mid, t, spread_for_math)
        mpp_raw = _clip(mpp_raw, self.mpp_clip)

        self._ema_obi = self._ema(self._ema_obi, obi_raw, self.feature_ema_alpha)
        self._ema_tfi = self._ema(self._ema_tfi, tfi_raw, self.feature_ema_alpha)
        self._ema_mpp = self._ema(self._ema_mpp, mpp_raw, self.feature_ema_alpha)

        obi = float(self._ema_obi)
        tfi = float(self._ema_tfi)
        mpp = float(self._ema_mpp)

        z_raw = self.sigmoid_bias + self.w_obi * obi_raw + self.w_tfi * tfi_raw + self.w_mpp * mpp_raw
        z = _clip(self.sigmoid_bias + self.w_obi * obi + self.w_tfi * tfi + self.w_mpp * mpp, self.z_clip)
        p_book_raw = _sigmoid(z_raw)
        self._ema_p_book = self._ema(self._ema_p_book, _sigmoid(z), self.prob_ema_alpha)
        p_book = float(self._ema_p_book)

        depth_bid = sum(float(q) for _, q in yes_bids[: self.obi_depth])
        depth_ask = sum(float(q) for _, q in yes_asks[: self.obi_depth])
        quality = self._quality_score(
            spread_cents=spread_for_math,
            depth_total=depth_bid + depth_ask,
            trade_count=trade_count,
            p_book_raw=p_book_raw,
            p_book_smoothed=p_book,
            now=t,
        )
        self._updates += 1

        return {
            "p_book": p_book,
            "p_book_raw": p_book_raw,
            "p_book_quality": quality,
            "reliability": quality,
            "obi": obi,
            "tfi": tfi,
            "mpp": mpp,
            "obi_raw": obi_raw,
            "tfi_raw": tfi_raw,
            "mpp_raw": mpp_raw,
            "trade_count": trade_count,
            "trade_volume": round(trade_volume, 6),
            "updates": self._updates,
            "yes_mid_cents": mid,
            "yes_spread_cents": spread_for_math,
            "z": z,
            "z_raw": z_raw,
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
    snapshot = _GLOBAL_MICRO.compute(yes_bids, yes_asks)
    now = float(snapshot.get("ts", time.time()))
    if not _GLOBAL_MICRO.should_emit(now):
        return None
    _GLOBAL_MICRO.mark_emitted(now)
    _LAST_P_BOOK = snapshot
    return snapshot


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
