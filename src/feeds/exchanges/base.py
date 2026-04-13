from __future__ import annotations

from abc import ABC, abstractmethod

from core.market_profiles import MarketProfile
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
