from feeds.exchanges.base import ExchangeAdapter, apply_book_update

EXCHANGE = "GEMINI"
CONNECT_KWARGS = {
    "max_size": 10_000_000,
    "open_timeout": 30,
    "ping_interval": 20,
    "ping_timeout": 10,
}


class GeminiAdapter(ExchangeAdapter):
    exchange = EXCHANGE
    connect_kwargs = CONNECT_KWARGS

    def build_url(self) -> str:
        return f"wss://api.gemini.com/v1/marketdata/{self.profile.gemini_symbol}"

    def handle_message(self, data: dict) -> bool:
        if data.get("type") != "update":
            return False

        parsed = False
        for event in data.get("events", []):
            if event.get("type") != "change":
                continue

            side_raw = event.get("side")
            if side_raw not in {"bid", "ask"}:
                continue

            side = "bids" if side_raw == "bid" else "asks"
            if apply_book_update(EXCHANGE, side, event.get("price"), event.get("remaining")):
                parsed = True

        return parsed
