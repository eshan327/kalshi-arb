from feeds.state.book_store import replace_full_book
from feeds.exchanges.base import ExchangeAdapter, add_snapshot_level, apply_book_update

EXCHANGE = "KRAKEN"
URL = "wss://ws.kraken.com/v2"
CONNECT_KWARGS = {
    "ping_interval": 20,
    "ping_timeout": 10,
}


class KrakenAdapter(ExchangeAdapter):
    exchange = EXCHANGE
    connect_kwargs = CONNECT_KWARGS

    def build_url(self) -> str:
        return URL

    def build_subscribe_message(self) -> dict:
        return {
            "method": "subscribe",
            "params": {
                "channel": "book",
                "symbol": [self.profile.kraken_symbol],
                "depth": 1000,
                "snapshot": True,
            },
        }

    def handle_message(self, data: dict) -> bool:
        if data.get("channel") != "book":
            return False

        msg_type = data.get("type")
        entries = data.get("data", [])

        if msg_type == "snapshot":
            snapshot_bids = {}
            snapshot_asks = {}

            for entry in entries:
                for level in entry.get("bids", []):
                    add_snapshot_level(snapshot_bids, level.get("price"), level.get("qty"))

                for level in entry.get("asks", []):
                    add_snapshot_level(snapshot_asks, level.get("price"), level.get("qty"))

            replace_full_book(EXCHANGE, snapshot_bids, snapshot_asks)
            return bool(snapshot_bids or snapshot_asks)

        parsed = False
        for entry in entries:
            if msg_type == "snapshot":
                continue

            for level in entry.get("bids", []):
                if apply_book_update(EXCHANGE, "bids", level.get("price"), level.get("qty")):
                    parsed = True

            for level in entry.get("asks", []):
                if apply_book_update(EXCHANGE, "asks", level.get("price"), level.get("qty")):
                    parsed = True

        return parsed
