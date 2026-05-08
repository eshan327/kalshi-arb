from __future__ import annotations

import time
from collections import deque
from threading import RLock
from typing import Any

from engine.orderbook import OrderBook
from engine.shadow.models import PaperPosition


class PaperLedger:
    """Ephemeral paper account ledger for shadow trading."""

    def __init__(self, *, starting_bankroll_usd: float) -> None:
        bankroll_cents = max(100, int(round(float(starting_bankroll_usd) * 100.0)))
        self._lock = RLock()
        self._starting_cash_cents = bankroll_cents
        self._cash_cents = bankroll_cents
        self._realized_pnl_cents = 0.0
        self._edge_captured_cents = 0.0
        self._fills_total = 0

        self._positions: dict[tuple[str, str], PaperPosition] = {}
        self._equity_curve: deque[dict[str, Any]] = deque(maxlen=5000)
        self._unrealized_curve: deque[dict[str, Any]] = deque(maxlen=5000)

    @staticmethod
    def _best_bid_by_side(book: OrderBook | None, side: str) -> float | None:
        if book is None or not book.initialized:
            return None
        yes_bid, _, no_bid, _ = book.get_best_prices()
        if side == "yes" and isinstance(yes_bid, (int, float)):
            return float(yes_bid)
        if side == "no" and isinstance(no_bid, (int, float)):
            return float(no_bid)
        return None

    def _positions_market_value_locked(self) -> float:
        total = 0.0
        for pos in self._positions.values():
            total += float(pos.contracts) * float(pos.last_mark_cents)
        return total

    def _unrealized_pnl_locked(self) -> float:
        total = 0.0
        for pos in self._positions.values():
            total += float(pos.contracts) * (float(pos.last_mark_cents) - float(pos.avg_entry_cents))
        return total

    def _record_curves_locked(self, ts: float) -> None:
        equity = float(self._cash_cents) + self._positions_market_value_locked()
        unrealized = self._unrealized_pnl_locked()
        self._equity_curve.append({"ts": ts, "value": round(equity, 6)})
        self._unrealized_curve.append({"ts": ts, "value": round(unrealized, 6)})

    def current_bankroll_cents(self) -> int:
        with self._lock:
            return int(round(float(self._cash_cents) + self._positions_market_value_locked()))

    def available_cash_cents(self) -> int:
        with self._lock:
            return int(self._cash_cents)

    def get_position_contracts(self, *, market_ticker: str, side: str) -> int:
        key = (str(market_ticker), str(side))
        with self._lock:
            pos = self._positions.get(key)
            return int(pos.contracts) if pos is not None else 0

    def apply_fill(
        self,
        *,
        market_ticker: str,
        side: str,
        contracts: int,
        fill_price_cents: int,
        fee_total_cents: float,
        expected_edge_cents: float,
        now_ts: float | None = None,
    ) -> dict[str, Any] | None:
        ts = time.time() if now_ts is None else float(now_ts)
        qty = max(1, int(contracts))
        px = max(1, min(99, int(fill_price_cents)))
        fees = max(0.0, float(fee_total_cents))

        with self._lock:
            total_cost = float(qty) * float(px) + fees
            if total_cost > float(self._cash_cents):
                return None

            self._cash_cents = int(round(float(self._cash_cents) - total_cost))
            key = (str(market_ticker), str(side))
            existing = self._positions.get(key)

            if existing is None:
                self._positions[key] = PaperPosition(
                    market_ticker=str(market_ticker),
                    side=str(side),
                    contracts=qty,
                    avg_entry_cents=float(px),
                    last_mark_cents=float(px),
                )
            else:
                new_contracts = int(existing.contracts) + qty
                weighted_sum = float(existing.avg_entry_cents) * float(existing.contracts) + float(px) * float(qty)
                existing.contracts = new_contracts
                existing.avg_entry_cents = weighted_sum / max(1.0, float(new_contracts))
                existing.last_mark_cents = float(px)

            self._fills_total += 1
            self._edge_captured_cents += float(expected_edge_cents)
            self._record_curves_locked(ts)

            updated = self._positions[key]
            return {
                "market_ticker": updated.market_ticker,
                "side": updated.side,
                "contracts": int(updated.contracts),
                "avg_entry_cents": round(float(updated.avg_entry_cents), 6),
                "fill_price_cents": int(px),
                "fee_total_cents": round(float(fees), 6),
                "cash_cents": int(self._cash_cents),
                "equity_cents": int(round(float(self._cash_cents) + self._positions_market_value_locked())),
            }

    def apply_close_fill(
        self,
        *,
        market_ticker: str,
        side: str,
        contracts: int,
        fill_price_cents: int,
        fee_total_cents: float,
        now_ts: float | None = None,
    ) -> dict[str, Any] | None:
        ts = time.time() if now_ts is None else float(now_ts)
        qty = max(1, int(contracts))
        px = max(1, min(99, int(fill_price_cents)))
        fees = max(0.0, float(fee_total_cents))

        key = (str(market_ticker), str(side))
        with self._lock:
            existing = self._positions.get(key)
            if existing is None or int(existing.contracts) <= 0:
                return None

            close_qty = min(int(existing.contracts), qty)
            avg_entry = float(existing.avg_entry_cents)

            proceeds = float(close_qty) * float(px) - fees
            realized_delta = float(close_qty) * (float(px) - avg_entry) - fees

            self._cash_cents = int(round(float(self._cash_cents) + proceeds))
            self._realized_pnl_cents += realized_delta

            existing.contracts = int(existing.contracts) - int(close_qty)
            existing.last_mark_cents = float(px)
            if existing.contracts <= 0:
                self._positions.pop(key, None)

            self._fills_total += 1
            self._record_curves_locked(ts)

            return {
                "market_ticker": str(market_ticker),
                "side": str(side),
                "closed_contracts": int(close_qty),
                "fill_price_cents": int(px),
                "fee_total_cents": round(float(fees), 6),
                "realized_delta_cents": int(round(realized_delta)),
                "cash_cents": int(self._cash_cents),
                "equity_cents": int(round(float(self._cash_cents) + self._positions_market_value_locked())),
            }

    def mark_to_market(self, *, market_ticker: str, book: OrderBook | None, now_ts: float | None = None) -> dict[str, Any]:
        ts = time.time() if now_ts is None else float(now_ts)

        with self._lock:
            for (ticker, side), pos in self._positions.items():
                if ticker != market_ticker:
                    continue
                mark = self._best_bid_by_side(book, side)
                if mark is not None:
                    pos.last_mark_cents = float(mark)

            self._record_curves_locked(ts)
            return {
                "unrealized_pnl_cents": int(round(self._unrealized_pnl_locked())),
                "equity_cents": int(round(float(self._cash_cents) + self._positions_market_value_locked())),
                "cash_cents": int(self._cash_cents),
            }

    def settle_market(
        self,
        *,
        market_ticker: str,
        strike_cents: float | None,
        settlement_price_cents: float | None,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        ts = time.time() if now_ts is None else float(now_ts)
        realized_delta = 0.0
        closed_contracts = 0

        with self._lock:
            keys = [key for key in self._positions if key[0] == str(market_ticker)]
            if not keys:
                self._record_curves_locked(ts)
                return {
                    "market_ticker": str(market_ticker),
                    "closed_contracts": 0,
                    "realized_delta_cents": 0,
                    "cash_cents": int(self._cash_cents),
                }

            strike = float(strike_cents) if isinstance(strike_cents, (int, float)) else None
            settlement = float(settlement_price_cents) if isinstance(settlement_price_cents, (int, float)) else None

            for key in keys:
                pos = self._positions.pop(key)
                closed_contracts += int(pos.contracts)

                if strike is None or settlement is None:
                    payout_cents = float(pos.last_mark_cents)
                else:
                    yes_wins = settlement >= strike
                    payout_cents = 100.0 if (pos.side == "yes") == yes_wins else 0.0

                realized_delta += float(pos.contracts) * (payout_cents - float(pos.avg_entry_cents))
                self._cash_cents = int(round(float(self._cash_cents) + float(pos.contracts) * payout_cents))

            self._realized_pnl_cents += realized_delta
            self._record_curves_locked(ts)

            return {
                "market_ticker": str(market_ticker),
                "closed_contracts": int(closed_contracts),
                "realized_delta_cents": int(round(realized_delta)),
                "cash_cents": int(self._cash_cents),
                "realized_pnl_cents": int(round(self._realized_pnl_cents)),
            }

    def snapshot(self, *, curve_limit: int = 1000) -> dict[str, Any]:
        with self._lock:
            positions = []
            for pos in self._positions.values():
                unrealized = float(pos.contracts) * (float(pos.last_mark_cents) - float(pos.avg_entry_cents))
                positions.append(
                    {
                        "market_ticker": pos.market_ticker,
                        "side": pos.side,
                        "contracts": int(pos.contracts),
                        "avg_entry_cents": round(float(pos.avg_entry_cents), 6),
                        "mark_cents": round(float(pos.last_mark_cents), 6),
                        "unrealized_pnl_cents": int(round(unrealized)),
                    }
                )

            unrealized_total = self._unrealized_pnl_locked()
            equity_cents = float(self._cash_cents) + self._positions_market_value_locked()

            return {
                "starting_cash_cents": int(self._starting_cash_cents),
                "cash_cents": int(self._cash_cents),
                "equity_cents": int(round(equity_cents)),
                "realized_pnl_cents": int(round(self._realized_pnl_cents)),
                "unrealized_pnl_cents": int(round(unrealized_total)),
                "edge_captured_cents": round(float(self._edge_captured_cents), 6),
                "fills_total": int(self._fills_total),
                "open_positions": positions,
                "equity_curve": list(self._equity_curve)[-max(1, int(curve_limit)):],
                "unrealized_curve": list(self._unrealized_curve)[-max(1, int(curve_limit)):],
            }
