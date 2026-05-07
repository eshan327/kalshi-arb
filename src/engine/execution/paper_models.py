from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from engine.execution.models import ExecutionIntent, OrderAction, SignalSide

PaperOrderStatus = Literal["resting", "partially_filled", "filled", "canceled", "rejected"]


@dataclass
class PaperOrder:
    order_id: str
    client_order_id: str
    market_ticker: str
    side: SignalSide
    action: OrderAction
    count: int
    remaining_count: int
    price_cents: int
    reserved_cents: int
    created_ts: float
    last_update_ts: float
    status: PaperOrderStatus
    edge_per_contract_cents: float
    confidence: float
    seconds_to_expiry: float
    execution_intent: ExecutionIntent = "maker"
    is_fallback_attempt: bool = False
    window_id: str | None = None


@dataclass
class PaperFill:
    fill_id: str
    order_id: str
    client_order_id: str
    market_ticker: str
    side: SignalSide
    action: OrderAction
    count: int
    price_cents: int
    is_taker: bool
    expected_edge_cents: float
    confidence: float
    seconds_to_expiry: float
    ts: float
    reason: str
    execution_intent: ExecutionIntent = "maker"
    is_fallback_attempt: bool = False
    fill_latency_ms: float | None = None
    window_id: str | None = None


@dataclass
class PaperPosition:
    market_ticker: str
    side: SignalSide
    count: int
    avg_price_cents: float
    last_mark_price_cents: float


@dataclass(frozen=True)
class SimulatedFillDecision:
    would_fill: bool
    fill_count: int
    fill_price_cents: int | None
    is_taker: bool
    reason: str
    fill_probability: float
    ts: float
    best_bid_cents: int | None = None
    best_ask_cents: int | None = None
    spread_cents: float | None = None
