from feeds.brti_state import mark_book_update_applied, replace_full_book, safe_float, update_level
from core.market_profiles import MarketProfile
from feeds.exchanges.base import ExchangeAdapter

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

            if event_type == "snapshot":
                snapshot_bids = {}
                snapshot_asks = {}
                for update in event.get("updates", []):
                    side_raw = update.get("side")
                    if side_raw not in {"bid", "ask", "offer"}:
                        continue

                    side = snapshot_bids if side_raw == "bid" else snapshot_asks
                    price = safe_float(update.get("price_level"))
                    qty = safe_float(update.get("new_quantity"))
                    if price is None or qty is None or price <= 0 or qty <= 0:
                        continue
                    side[price] = qty

                replace_full_book(EXCHANGE, snapshot_bids, snapshot_asks)
                parsed = True
                continue

            for update in event.get("updates", []):
                side_raw = update.get("side")
                if side_raw not in {"bid", "ask", "offer"}:
                    continue

                side = "bids" if side_raw == "bid" else "asks"
                price = safe_float(update.get("price_level"))
                qty = safe_float(update.get("new_quantity"))
                if price is None or qty is None or price <= 0:
                    continue

                update_level(EXCHANGE, side, price, qty)
                mark_book_update_applied()
                parsed = True

        return parsed


async def stream(profile: MarketProfile) -> None:
    await CoinbaseAdapter(profile).stream()
