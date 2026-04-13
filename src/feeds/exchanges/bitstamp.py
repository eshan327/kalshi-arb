from feeds.brti_state import replace_full_book, safe_float
from core.market_profiles import MarketProfile
from feeds.exchanges.base import ExchangeAdapter

EXCHANGE = "BITSTAMP"
URL = "wss://ws.bitstamp.net"
CONNECT_KWARGS = {
    "ping_interval": 20,
    "ping_timeout": 10,
}


class BitstampAdapter(ExchangeAdapter):
    exchange = EXCHANGE
    connect_kwargs = CONNECT_KWARGS

    def build_url(self) -> str:
        return URL

    def build_subscribe_message(self) -> dict:
        return {
            "event": "bts:subscribe",
            "data": {"channel": self.profile.bitstamp_channel},
        }

    def handle_message(self, data: dict) -> bool:
        if data.get("event") != "data":
            return False

        orderbook = data.get("data", {})
        snapshot_bids = {}
        snapshot_asks = {}

        for price_str, size_str in orderbook.get("bids", []):
            price = safe_float(price_str)
            size = safe_float(size_str)
            if price is None or size is None or price <= 0 or size <= 0:
                continue
            snapshot_bids[price] = size

        for price_str, size_str in orderbook.get("asks", []):
            price = safe_float(price_str)
            size = safe_float(size_str)
            if price is None or size is None or price <= 0 or size <= 0:
                continue
            snapshot_asks[price] = size

        replace_full_book(EXCHANGE, snapshot_bids, snapshot_asks)
        return True


async def stream(profile: MarketProfile) -> None:
    await BitstampAdapter(profile).stream()
