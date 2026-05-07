from __future__ import annotations

from dataclasses import dataclass

from core.config import (
    EXECUTION_MAX_DAILY_LOSS_CENTS,
    EXECUTION_MAX_MARKET_CONTRACTS,
    EXECUTION_MAX_OPEN_ORDERS,
    EXECUTION_MAX_ORDER_CONTRACTS,
    EXECUTION_MIN_CASH_BUFFER_CENTS,
)
from engine.execution.models import ExecutionSignal


@dataclass(frozen=True)
class RiskDecision:
    ok: bool
    reason: str


class ExecutionRiskGuard:
    """Single-responsibility guardrail checks before any order submission."""

    def can_place(
        self,
        *,
        signal: ExecutionSignal,
        market_position_contracts: int,
        resting_market_order_contracts: int,
        open_orders_total: int,
        available_balance_cents: int,
        daily_realized_pnl_cents: int,
    ) -> RiskDecision:
        if signal.count > EXECUTION_MAX_ORDER_CONTRACTS:
            return RiskDecision(ok=False, reason="per_order_limit")

        if open_orders_total >= EXECUTION_MAX_OPEN_ORDERS:
            return RiskDecision(ok=False, reason="max_open_orders")

        projected_abs_market = (
            abs(int(market_position_contracts))
            + max(0, int(resting_market_order_contracts))
            + int(signal.count)
        )
        if projected_abs_market > EXECUTION_MAX_MARKET_CONTRACTS:
            return RiskDecision(ok=False, reason="per_market_position_limit")

        if int(daily_realized_pnl_cents) <= -abs(int(EXECUTION_MAX_DAILY_LOSS_CENTS)):
            return RiskDecision(ok=False, reason="daily_loss_limit")

        est_cost_cents = int(signal.quote_price_cents) * int(signal.count)
        projected_available = int(available_balance_cents) - est_cost_cents
        if projected_available < int(EXECUTION_MIN_CASH_BUFFER_CENTS):
            return RiskDecision(ok=False, reason="cash_buffer")

        return RiskDecision(ok=True, reason="ok")
