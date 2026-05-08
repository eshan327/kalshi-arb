from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SignalSide = Literal["yes", "no"]
ExecutionIntent = Literal["taker", "maker"]
SignalAction = Literal["buy", "sell"]


@dataclass(frozen=True)
class ShadowSignal:
    ts: float
    market_ticker: str
    side: SignalSide
    action: SignalAction
    intent: ExecutionIntent
    count: int
    quote_price_cents: int
    fair_price_cents: float
    edge_cents: float
    edge_probability: float
    confidence: float
    model_probability: float
    market_implied_probability: float
    reason: str
    diagnostics: dict[str, float | int | str | None]


@dataclass
class PaperPosition:
    market_ticker: str
    side: SignalSide
    contracts: int
    avg_entry_cents: float
    last_mark_cents: float


@dataclass(frozen=True)
class PaperFillQuote:
    ts: float
    can_fill: bool
    reason: str
    side: SignalSide
    action: SignalAction
    best_bid_cents: int | None
    best_ask_cents: int | None
    spread_cents: float | None
    fill_price_cents: int | None
    slippage_cents: float
