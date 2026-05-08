from __future__ import annotations

import time
from typing import Any


def build_shadow_event(
    *,
    kind: str,
    reason: str,
    intent: str,
    market_ticker: str,
    side: str | None,
    count: int | None,
    fill_price_cents: int | None,
    slippage_cents: float,
    is_synthetic_fill: bool,
    now_ts: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ts = time.time() if now_ts is None else float(now_ts)
    payload = {
        "ts": ts,
        "kind": str(kind),
        "reason": str(reason),
        "intent": str(intent),
        "market_ticker": str(market_ticker),
        "side": None if side is None else str(side),
        "count": None if count is None else int(count),
        "fill_price_cents": None if fill_price_cents is None else int(fill_price_cents),
        "slippage_cents": round(float(slippage_cents), 6),
        "is_synthetic_fill": bool(is_synthetic_fill),
    }
    if isinstance(extra, dict):
        payload.update(extra)
    return payload
