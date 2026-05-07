from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SignalSide = Literal["yes", "no"]
OrderAction = Literal["buy", "sell"]
ExecutionIntent = Literal["maker", "taker"]


@dataclass(frozen=True)
class ExecutionSignal:
    ts: float
    market_ticker: str
    side: SignalSide
    action: OrderAction
    execution_intent: ExecutionIntent
    is_fallback_attempt: bool
    policy_profile: str | None
    quote_price_cents: int
    fair_price_cents: float
    edge_cents: float
    confidence: float
    p_model: float
    p_book: float | None
    p_book_quality: float | None
    p_book_alignment: str | None
    seconds_to_expiry: float
    timing_score: float
    count: int
    reason: str


@dataclass
class ManagedOrder:
    order_id: str
    client_order_id: str
    market_ticker: str
    side: SignalSide
    action: OrderAction
    count: int
    price_cents: int
    created_ts: float
    last_update_ts: float
    reprices: int = 0
