from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SignalSide = Literal["yes", "no"]
SignalAction = Literal["buy", "sell"]


@dataclass(frozen=True)
class TradeSignal:
    ts: float
    market_ticker: str
    side: SignalSide
    action: SignalAction
    count: int
    quote_price_cents: float
    fair_price_cents: float
    credit_cents: float
    edge_cents: float
    edge_probability: float
    confidence: float
    model_probability: float
    market_implied_probability: float
    reason: str
    diagnostics: dict[str, float | int | str | bool | None]
