from __future__ import annotations

import time
from collections import deque
from threading import RLock
from typing import Any

from core.config import EXECUTION_EVENTS_MAXLEN, PAPER_SIM_STARTING_CASH_CENTS
from engine.execution.models import ExecutionSignal
from engine.execution.paper_models import PaperFill, PaperOrder, PaperPosition
from engine.orderbook import OrderBook


def _utc_day(ts: float) -> int:
    return int(float(ts) // 86400)


class PaperAccount:
    def __init__(self) -> None:
        self._lock = RLock()
        self._starting_cash_cents = int(PAPER_SIM_STARTING_CASH_CENTS)
        self._cash_cents = int(PAPER_SIM_STARTING_CASH_CENTS)
        self._realized_pnl_cents = 0
        self._daily_realized_pnl_cents = 0
        self._daily_pnl_day = _utc_day(time.time())
        self._edge_captured_cents = 0.0
        self._settled_wins = 0
        self._settled_losses = 0
        self._settled_trades = 0
        self._fill_count = 0
        self._order_seq = 0
        self._fill_seq = 0

        self._open_orders: dict[str, PaperOrder] = {}
        self._positions: dict[tuple[str, str], PaperPosition] = {}
        self._marks: dict[tuple[str, str], float] = {}

        maxlen = max(500, int(EXECUTION_EVENTS_MAXLEN))
        self._pnl_curve: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._edge_curve: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._win_rate_curve: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._unrealized_curve: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def _roll_daily_bucket(self, ts: float) -> None:
        day = _utc_day(ts)
        if day == self._daily_pnl_day:
            return
        self._daily_pnl_day = day
        self._daily_realized_pnl_cents = 0

    def _reserve_for_order(self, count: int, price_cents: int) -> int:
        return max(0, int(count)) * max(1, int(price_cents))

    def _next_order_id(self, now_ts: float) -> str:
        self._order_seq += 1
        return f"paper-order-{int(now_ts * 1000)}-{self._order_seq}"

    def _next_fill_id(self, now_ts: float) -> str:
        self._fill_seq += 1
        return f"paper-fill-{int(now_ts * 1000)}-{self._fill_seq}"

    def _reserved_cents(self) -> int:
        return sum(max(0, int(order.reserved_cents)) for order in self._open_orders.values())

    def _position_cost_basis_cents(self) -> float:
        return sum(float(pos.count) * float(pos.avg_price_cents) for pos in self._positions.values())

    def _position_mark_value_cents(self) -> float:
        total = 0.0
        for key, pos in self._positions.items():
            mark = self._marks.get(key)
            if mark is None:
                mark = pos.avg_price_cents
            total += float(pos.count) * float(mark)
        return total

    def _compute_unrealized_locked(self) -> int:
        value = self._position_mark_value_cents()
        basis = self._position_cost_basis_cents()
        return int(round(value - basis))

    def list_open_orders(self, market_ticker: str | None = None) -> list[PaperOrder]:
        with self._lock:
            if market_ticker is None:
                return [order for order in self._open_orders.values()]
            return [
                order
                for order in self._open_orders.values()
                if order.market_ticker == market_ticker
            ]

    def get_open_orders_total(self) -> int:
        with self._lock:
            return len(self._open_orders)

    def get_resting_contracts(self, market_ticker: str) -> int:
        with self._lock:
            return sum(
                int(order.remaining_count)
                for order in self._open_orders.values()
                if order.market_ticker == market_ticker
            )

    def get_market_position_contracts(self, market_ticker: str) -> int:
        with self._lock:
            return sum(
                int(position.count)
                for (ticker, _), position in self._positions.items()
                if ticker == market_ticker
            )

    def get_available_balance_cents(self) -> int:
        with self._lock:
            return int(self._cash_cents)

    def get_daily_realized_pnl_cents(self) -> int:
        with self._lock:
            self._roll_daily_bucket(time.time())
            return int(self._daily_realized_pnl_cents)

    def place_order(
        self,
        signal: ExecutionSignal,
        now_ts: float | None = None,
        *,
        window_id: str | None = None,
    ) -> PaperOrder | None:
        ts = time.time() if now_ts is None else float(now_ts)
        reserve = self._reserve_for_order(signal.count, signal.quote_price_cents)

        with self._lock:
            if reserve > self._cash_cents:
                return None

            self._cash_cents -= reserve
            order_id = self._next_order_id(ts)
            order = PaperOrder(
                order_id=order_id,
                client_order_id=order_id,
                market_ticker=signal.market_ticker,
                side=signal.side,
                action=signal.action,
                count=int(signal.count),
                remaining_count=int(signal.count),
                price_cents=int(signal.quote_price_cents),
                reserved_cents=reserve,
                created_ts=ts,
                last_update_ts=ts,
                status="resting",
                edge_per_contract_cents=float(signal.edge_cents),
                confidence=float(signal.confidence),
                seconds_to_expiry=float(signal.seconds_to_expiry),
                execution_intent=signal.execution_intent,
                is_fallback_attempt=bool(signal.is_fallback_attempt),
                window_id=window_id,
            )
            self._open_orders[order_id] = order
            return order

    def cancel_order(self, order_id: str, reason: str, now_ts: float | None = None) -> PaperOrder | None:
        ts = time.time() if now_ts is None else float(now_ts)
        with self._lock:
            order = self._open_orders.pop(order_id, None)
            if order is None:
                return None

            self._cash_cents += max(0, int(order.reserved_cents))
            order.reserved_cents = 0
            order.last_update_ts = ts
            order.status = "canceled"
            if reason == "reject":
                order.status = "rejected"
            return order

    def cancel_orders(self, orders: list[PaperOrder], reason: str, now_ts: float | None = None) -> int:
        canceled = 0
        for order in orders:
            if self.cancel_order(order.order_id, reason=reason, now_ts=now_ts) is not None:
                canceled += 1
        return canceled

    def apply_fill(
        self,
        *,
        order: PaperOrder,
        fill_count: int,
        fill_price_cents: int,
        is_taker: bool,
        reason: str,
        now_ts: float | None = None,
    ) -> PaperFill:
        ts = time.time() if now_ts is None else float(now_ts)
        count = max(1, min(int(fill_count), int(order.remaining_count)))
        px = max(1, min(99, int(fill_price_cents)))

        with self._lock:
            current = self._open_orders.get(order.order_id)
            if current is None:
                current = order

            reserved_release = count * int(current.price_cents)
            actual_cost = count * px
            current.remaining_count = max(0, int(current.remaining_count) - count)
            current.reserved_cents = max(0, int(current.reserved_cents) - reserved_release)
            current.last_update_ts = ts
            current.status = "filled" if current.remaining_count == 0 else "partially_filled"

            # Reserved budget is returned at quote, then execution cost is charged at fill price.
            self._cash_cents += reserved_release
            self._cash_cents -= actual_cost

            if current.remaining_count == 0:
                self._open_orders.pop(current.order_id, None)

            key = (current.market_ticker, current.side)
            pos = self._positions.get(key)
            if pos is None:
                pos = PaperPosition(
                    market_ticker=current.market_ticker,
                    side=current.side,
                    count=count,
                    avg_price_cents=float(px),
                    last_mark_price_cents=float(px),
                )
                self._positions[key] = pos
            else:
                new_count = int(pos.count) + count
                weighted = float(pos.avg_price_cents) * float(pos.count) + float(px) * float(count)
                pos.count = new_count
                pos.avg_price_cents = weighted / max(1, new_count)

            self._marks[key] = float(px)
            self._fill_count += 1

            expected_edge = float(current.edge_per_contract_cents) * float(count)
            self._edge_captured_cents += expected_edge
            self._edge_curve.append({"ts": ts, "value": round(self._edge_captured_cents, 6)})

            unrealized = self._compute_unrealized_locked()
            self._unrealized_curve.append({"ts": ts, "value": unrealized})

            fill = PaperFill(
                fill_id=self._next_fill_id(ts),
                order_id=current.order_id,
                client_order_id=current.client_order_id,
                market_ticker=current.market_ticker,
                side=current.side,
                action=current.action,
                count=count,
                price_cents=px,
                is_taker=bool(is_taker),
                expected_edge_cents=round(expected_edge, 6),
                confidence=float(current.confidence),
                seconds_to_expiry=float(current.seconds_to_expiry),
                ts=ts,
                reason=reason,
                execution_intent=current.execution_intent,
                is_fallback_attempt=bool(current.is_fallback_attempt),
                fill_latency_ms=round(max(0.0, (ts - float(current.created_ts)) * 1000.0), 3),
                window_id=current.window_id,
            )
            return fill

    def mark_to_market(self, market_ticker: str, book: OrderBook | None, now_ts: float | None = None) -> int:
        ts = time.time() if now_ts is None else float(now_ts)

        with self._lock:
            if book is not None and book.initialized:
                yes_bid, _, no_bid, _ = book.get_best_prices()
                if isinstance(yes_bid, (int, float)):
                    self._marks[(market_ticker, "yes")] = float(yes_bid)
                if isinstance(no_bid, (int, float)):
                    self._marks[(market_ticker, "no")] = float(no_bid)

            unrealized = self._compute_unrealized_locked()
            self._unrealized_curve.append({"ts": ts, "value": unrealized})
            return unrealized

    def settle_market(
        self,
        *,
        market_ticker: str,
        strike_cents: int | None,
        settlement_price_cents: float | None,
        now_ts: float | None = None,
    ) -> int:
        ts = time.time() if now_ts is None else float(now_ts)

        with self._lock:
            self._roll_daily_bucket(ts)
            realized_delta_total = 0
            close_keys = [key for key in self._positions if key[0] == market_ticker]

            if not close_keys:
                return 0

            settlement_is_known = False
            strike_value = 0.0
            settlement_value = 0.0

            if isinstance(strike_cents, int) and strike_cents > 0 and isinstance(
                settlement_price_cents,
                (int, float),
            ):
                settlement_is_known = True
                strike_value = float(strike_cents)
                settlement_value = float(settlement_price_cents)

            yes_resolves = False
            if settlement_is_known:
                yes_resolves = settlement_value >= strike_value

            for key in close_keys:
                pos = self._positions.pop(key)

                if settlement_is_known:
                    payout = 100 if (pos.side == "yes") == yes_resolves else 0
                    exit_px = float(payout)
                else:
                    exit_px = float(self._marks.get(key, pos.avg_price_cents))

                delta = int(round(float(pos.count) * (exit_px - float(pos.avg_price_cents))))
                realized_delta_total += delta
                self._cash_cents += int(round(float(pos.count) * exit_px))

                self._settled_trades += 1
                if delta > 0:
                    self._settled_wins += 1
                elif delta < 0:
                    self._settled_losses += 1

            self._realized_pnl_cents += realized_delta_total
            self._daily_realized_pnl_cents += realized_delta_total
            self._pnl_curve.append({"ts": ts, "value": self._realized_pnl_cents})

            win_rate = (
                float(self._settled_wins) / float(self._settled_trades)
                if self._settled_trades > 0
                else 0.0
            )
            self._win_rate_curve.append({"ts": ts, "value": round(win_rate, 6)})

            unrealized = self._compute_unrealized_locked()
            self._unrealized_curve.append({"ts": ts, "value": unrealized})
            return realized_delta_total

    def snapshot(self, curve_limit: int = 1200) -> dict[str, Any]:
        with self._lock:
            reserved = self._reserved_cents()
            unrealized = self._compute_unrealized_locked()
            mark_value = self._position_mark_value_cents()
            equity = int(round(self._cash_cents + reserved + mark_value))
            win_rate = (
                float(self._settled_wins) / float(self._settled_trades)
                if self._settled_trades > 0
                else None
            )

            positions = []
            for key, pos in self._positions.items():
                mark_px = float(self._marks.get(key, pos.avg_price_cents))
                positions.append(
                    {
                        "market_ticker": pos.market_ticker,
                        "side": pos.side,
                        "count": int(pos.count),
                        "avg_price_cents": round(float(pos.avg_price_cents), 4),
                        "mark_price_cents": round(mark_px, 4),
                        "unrealized_pnl_cents": int(round(float(pos.count) * (mark_px - float(pos.avg_price_cents)))),
                    }
                )

            return {
                "starting_cash_cents": int(self._starting_cash_cents),
                "cash_cents": int(self._cash_cents),
                "reserved_cents": int(reserved),
                "available_balance_cents": int(self._cash_cents),
                "equity_cents": equity,
                "realized_pnl_cents": int(self._realized_pnl_cents),
                "daily_realized_pnl_cents": int(self._daily_realized_pnl_cents),
                "unrealized_pnl_cents": int(unrealized),
                "edge_captured_cents": round(float(self._edge_captured_cents), 6),
                "fill_count": int(self._fill_count),
                "settled_trades": int(self._settled_trades),
                "wins": int(self._settled_wins),
                "losses": int(self._settled_losses),
                "win_rate": win_rate,
                "open_orders": int(len(self._open_orders)),
                "positions": positions,
                "pnl_curve": list(self._pnl_curve)[-curve_limit:],
                "edge_curve": list(self._edge_curve)[-curve_limit:],
                "win_rate_curve": list(self._win_rate_curve)[-curve_limit:],
                "unrealized_curve": list(self._unrealized_curve)[-curve_limit:],
            }
