from feeds.state.book_store import replace_full_book
from feeds.exchanges.base import ExchangeAdapter, add_snapshot_level, apply_book_update

EXCHANGE = "COINBASE"
URL = "wss://advanced-trade-ws.coinbase.com"
CONNECT_KWARGS = {
    "max_size": 50_000_000,
    "compression": None,
    "ping_interval": 20,
    "ping_timeout": 10,
}


class CoinbaseAdapter(ExchangeAdapter):
    exchange = EXCHANGE
    connect_kwargs = CONNECT_KWARGS

    def build_url(self) -> str:
        return URL

    def build_subscribe_message(self) -> dict:
        return {
            "type": "subscribe",
            "product_ids": [self.profile.coinbase_product_id],
            "channel": "level2",
        }

    def handle_message(self, data: dict) -> bool:
        if data.get("channel") != "l2_data":
            return False

        parsed = False
        for event in data.get("events", []):
            event_type = event.get("type")
            updates = event.get("updates", [])

            if event_type == "snapshot":
                snapshot_bids = {}
                snapshot_asks = {}
                for update in updates:
                    side_raw = update.get("side")
                    if side_raw not in {"bid", "ask", "offer"}:
                        continue

                    side = snapshot_bids if side_raw == "bid" else snapshot_asks
                    add_snapshot_level(side, update.get("price_level"), update.get("new_quantity"))

                replace_full_book(EXCHANGE, snapshot_bids, snapshot_asks)
                parsed = True
                continue

            for update in updates:
                side_raw = update.get("side")
                if side_raw not in {"bid", "ask", "offer"}:
                    continue

                side = "bids" if side_raw == "bid" else "asks"
                if apply_book_update(EXCHANGE, side, update.get("price_level"), update.get("new_quantity")):
                    parsed = True

        return parsed
