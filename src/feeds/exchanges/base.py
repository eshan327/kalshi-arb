from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from core.market_profiles import MarketProfile
from feeds.state.book_store import safe_float, update_level
from feeds.state.diagnostics_store import mark_book_update_applied
from feeds.exchanges.runtime import run_exchange_stream


class ExchangeAdapter(ABC):
    exchange: str
    connect_kwargs: dict

    def __init__(self, profile: MarketProfile):
        self.profile = profile

    @abstractmethod
    def build_url(self) -> str:
        raise NotImplementedError

    def build_subscribe_message(self) -> dict | None:
        return None

    @abstractmethod
    def handle_message(self, data: dict) -> bool:
        raise NotImplementedError

    async def stream(self) -> None:
        await run_exchange_stream(
            exchange=self.exchange,
            url=self.build_url(),
            handle_message=self.handle_message,
            subscribe_message=self.build_subscribe_message(),
            connect_kwargs=self.connect_kwargs,
        )


def _parse_level(price_raw: Any, size_raw: Any, *, require_positive_size: bool) -> tuple[float, float] | None:
    price = safe_float(price_raw)
    size = safe_float(size_raw)
    if price is None or size is None or price <= 0:
        return None
    if require_positive_size and size <= 0:
        return None
    return price, size


def add_snapshot_level(side_book: dict[float, float], price_raw: Any, size_raw: Any) -> bool:
    parsed = _parse_level(price_raw, size_raw, require_positive_size=True)
    if parsed is None:
        return False
    price, size = parsed
    side_book[price] = size
    return True


def apply_book_update(exchange: str, side: str, price_raw: Any, size_raw: Any) -> bool:
    parsed = _parse_level(price_raw, size_raw, require_positive_size=False)
    if parsed is None:
        return False
    price, size = parsed
    update_level(exchange, side, price, size)
    mark_book_update_applied()
    return True
