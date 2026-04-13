from feeds.state.book_store import replace_full_book
from feeds.exchanges.base import ExchangeAdapter, add_snapshot_level, apply_book_update

EXCHANGE = "PAXOS"
CONNECT_KWARGS = {
    "max_size": 10_000_000,
    "ping_interval": 20,
    "ping_timeout": 10,
}


class PaxosAdapter(ExchangeAdapter):
    exchange = EXCHANGE
    connect_kwargs = CONNECT_KWARGS

    def build_url(self) -> str:
        return f"wss://ws.paxos.com/marketdata/{self.profile.paxos_symbol}"

    def handle_message(self, data: dict) -> bool:
        msg_type = data.get("type")

        if msg_type == "SNAPSHOT":
            snapshot_bids = {}
            snapshot_asks = {}

            for level in data.get("bids", []):
                add_snapshot_level(snapshot_bids, level.get("price"), level.get("amount"))

            for level in data.get("asks", []):
                add_snapshot_level(snapshot_asks, level.get("price"), level.get("amount"))

            replace_full_book(EXCHANGE, snapshot_bids, snapshot_asks)
            return True

        if msg_type == "UPDATE":
            side_raw = data.get("side")
            if side_raw not in {"BUY", "SELL"}:
                return False

            side = "bids" if side_raw == "BUY" else "asks"
            return apply_book_update(EXCHANGE, side, data.get("price"), data.get("amount"))

        return False
