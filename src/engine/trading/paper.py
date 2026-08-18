from __future__ import annotations

import time
import uuid
from threading import RLock
from typing import Any

from engine.orderbook import OrderBook
from engine.trading.fees import taker_fee_cents_per_contract


class PaperAccount:
    """Ephemeral IOC paper fills against the live top of book."""

    # ponytail: no latency model; add recorded replay if paper/live fills diverge.

    def __init__(self, starting_cash_cents: int) -> None:
        self._lock = RLock()
        self._cash_cents = float(max(100, starting_cash_cents))
        self._positions: dict[tuple[str, str], dict[str, Any]] = {}
        self._realized_pnl_cents = 0.0

    def _market_value(self) -> float:
        return sum(
            float(position["contracts"]) * float(position["mark_cents"])
            for position in self._positions.values()
        )

    def market_tickers(self) -> set[str]:
        with self._lock:
            return {ticker for ticker, _ in self._positions}

    def mark_to_market(self, market_ticker: str, book: OrderBook | None) -> None:
        if book is None or not book.initialized or book.market_ticker != market_ticker:
            return
        yes_bid, _, no_bid, _ = book.get_best_prices()
        with self._lock:
            for side, mark in (("yes", yes_bid), ("no", no_bid)):
                position = self._positions.get((market_ticker, side))
                if position is not None and isinstance(mark, (int, float)):
                    position["mark_cents"] = float(mark)

    def place_ioc(
        self,
        *,
        market_ticker: str,
        side: str,
        action: str,
        count: int,
        price_cents: float,
        book: OrderBook | None,
        fee_multiplier: float = 1.0,
    ) -> dict[str, Any]:
        order_id = f"paper-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        if book is None or not book.initialized or book.market_ticker != market_ticker:
            return self._order_result(
                order_id, market_ticker, side, action, 0, None, "unfilled"
            )

        yes_bids, yes_asks, no_bids, no_asks = book.get_orderbook_top_n(1)
        fill_level = (
            (yes_asks if side == "yes" else no_asks)
            if action == "buy"
            else (yes_bids if side == "yes" else no_bids)
        )
        fill_price = fill_level[0][0] if fill_level else None
        marketable = isinstance(fill_price, (int, float)) and (
            float(fill_price) <= price_cents
            if action == "buy"
            else float(fill_price) >= price_cents
        )
        if not marketable:
            return self._order_result(
                order_id, market_ticker, side, action, 0, fill_price, "unfilled"
            )

        quantity = min(max(1, int(count)), int(fill_level[0][1]))
        if quantity <= 0:
            return self._order_result(
                order_id, market_ticker, side, action, 0, fill_price, "unfilled"
            )
        price = float(fill_price)
        fees = (
            taker_fee_cents_per_contract(
                price,
                count=quantity,
                fee_multiplier=fee_multiplier,
                action=action,
            )
            * quantity
        )
        key = (market_ticker, side)

        with self._lock:
            if action == "buy":
                cost = quantity * price + fees
                if cost > self._cash_cents:
                    return self._order_result(
                        order_id, market_ticker, side, action, 0, price, "rejected"
                    )
                self._cash_cents -= cost
                position = self._positions.get(key)
                if position is None:
                    self._positions[key] = {
                        "contracts": quantity,
                        "avg_entry_cents": price,
                        "mark_cents": price,
                        "fees_paid_cents": fees,
                    }
                else:
                    old_quantity = int(position["contracts"])
                    new_quantity = old_quantity + quantity
                    position["avg_entry_cents"] = (
                        old_quantity * float(position["avg_entry_cents"])
                        + quantity * price
                    ) / new_quantity
                    position["contracts"] = new_quantity
                    position["mark_cents"] = price
                    position["fees_paid_cents"] += fees
            else:
                position = self._positions.get(key)
                if position is None:
                    return self._order_result(
                        order_id, market_ticker, side, action, 0, price, "rejected"
                    )
                old_quantity = int(position["contracts"])
                quantity = min(quantity, old_quantity)
                fees = (
                    taker_fee_cents_per_contract(
                        price,
                        count=quantity,
                        fee_multiplier=fee_multiplier,
                        action=action,
                    )
                    * quantity
                )
                entry_fees = (
                    float(position["fees_paid_cents"]) * quantity / old_quantity
                )
                self._cash_cents += quantity * price - fees
                self._realized_pnl_cents += (
                    quantity * (price - float(position["avg_entry_cents"]))
                    - fees
                    - entry_fees
                )
                position["contracts"] -= quantity
                position["fees_paid_cents"] -= entry_fees
                if position["contracts"] <= 0:
                    self._positions.pop(key)

        return self._order_result(
            order_id, market_ticker, side, action, quantity, price, "executed"
        )

    @staticmethod
    def _order_result(
        order_id: str,
        market_ticker: str,
        side: str,
        action: str,
        fill_count: int,
        fill_price: float | None,
        status: str,
    ) -> dict[str, Any]:
        return {
            "ok": status != "rejected",
            "client_order_id": order_id,
            "order": {
                "order_id": order_id,
                "ticker": market_ticker,
                "side": side,
                "action": action,
                "status": status,
                "fill_count": str(fill_count),
                "fill_price_cents": fill_price,
            },
        }

    def settle(self, market_ticker: str, result: str) -> dict[str, Any]:
        outcome = result.strip().lower()
        if outcome not in {"yes", "no"}:
            return {"closed_contracts": 0}
        closed = 0
        realized = 0.0
        with self._lock:
            for key in [key for key in self._positions if key[0] == market_ticker]:
                position = self._positions.pop(key)
                quantity = int(position["contracts"])
                payout = 100.0 if key[1] == outcome else 0.0
                closed += quantity
                self._cash_cents += quantity * payout
                realized += quantity * (
                    payout - float(position["avg_entry_cents"])
                ) - float(position["fees_paid_cents"])
            self._realized_pnl_cents += realized
        return {
            "market_ticker": market_ticker,
            "result": outcome,
            "closed_contracts": closed,
            "realized_pnl_cents": round(realized, 4),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            positions = []
            for (ticker, side), position in self._positions.items():
                quantity = int(position["contracts"])
                avg = float(position["avg_entry_cents"])
                positions.append(
                    {
                        "market_ticker": ticker,
                        "side": side,
                        "contracts": quantity,
                        "strategy_contracts": quantity,
                        "avg_entry_cents": round(avg, 4),
                        "market_exposure_cents": round(quantity * avg, 4),
                        "realized_pnl_cents": 0,
                        "fees_paid_cents": round(float(position["fees_paid_cents"]), 4),
                    }
                )
            market_value = self._market_value()
            return {
                "cash_cents": round(self._cash_cents),
                "portfolio_value_cents": round(market_value),
                "equity_cents": round(self._cash_cents + market_value),
                "realized_pnl_cents": round(self._realized_pnl_cents, 4),
                "positions": positions,
                "updated_ts": int(time.time()),
                "refreshed_ts": time.time(),
            }
