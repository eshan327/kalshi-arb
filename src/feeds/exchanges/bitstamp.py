from feeds.state.book_store import replace_full_book
from feeds.exchanges.base import ExchangeAdapter, add_snapshot_level

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
            add_snapshot_level(snapshot_bids, price_str, size_str)

        for price_str, size_str in orderbook.get("asks", []):
            add_snapshot_level(snapshot_asks, price_str, size_str)

        replace_full_book(EXCHANGE, snapshot_bids, snapshot_asks)
        return True
