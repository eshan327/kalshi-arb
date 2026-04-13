from feeds.brti_state import mark_book_update_applied, replace_full_book, safe_float, update_level
from core.market_profiles import MarketProfile
from feeds.exchanges.base import ExchangeAdapter

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
        parsed = False

        if msg_type == "snapshot":
            snapshot_bids = {}
            snapshot_asks = {}

            for entry in data.get("data", []):
                for level in entry.get("bids", []):
                    price = safe_float(level.get("price"))
                    qty = safe_float(level.get("qty"))
                    if price is None or qty is None or price <= 0 or qty <= 0:
                        continue
                    snapshot_bids[price] = qty

                for level in entry.get("asks", []):
                    price = safe_float(level.get("price"))
                    qty = safe_float(level.get("qty"))
                    if price is None or qty is None or price <= 0 or qty <= 0:
                        continue
                    snapshot_asks[price] = qty

            replace_full_book(EXCHANGE, snapshot_bids, snapshot_asks)
            parsed = bool(snapshot_bids or snapshot_asks)

        for entry in data.get("data", []):
            if msg_type == "snapshot":
                continue

            for level in entry.get("bids", []):
                price = safe_float(level.get("price"))
                qty = safe_float(level.get("qty"))
                if price is None or qty is None or price <= 0:
                    continue
                update_level(EXCHANGE, "bids", price, qty)
                mark_book_update_applied()
                parsed = True

            for level in entry.get("asks", []):
                price = safe_float(level.get("price"))
                qty = safe_float(level.get("qty"))
                if price is None or qty is None or price <= 0:
                    continue
                update_level(EXCHANGE, "asks", price, qty)
                mark_book_update_applied()
                parsed = True

        return parsed


async def stream(profile: MarketProfile) -> None:
    await KrakenAdapter(profile).stream()
