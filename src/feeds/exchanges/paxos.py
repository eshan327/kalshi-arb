from feeds.brti_state import mark_book_update_applied, replace_full_book, safe_float, update_level
from feeds.exchanges.runtime import run_exchange_stream

EXCHANGE = "PAXOS"
URL = "wss://ws.paxos.com/marketdata/BTCUSD"
CONNECT_KWARGS = {
    "max_size": 10_000_000,
    "ping_interval": 20,
    "ping_timeout": 10,
}


def _handle_message(data: dict) -> bool:
    msg_type = data.get("type")

    if msg_type == "SNAPSHOT":
        snapshot_bids = {}
        snapshot_asks = {}

        for level in data.get("bids", []):
            price = safe_float(level.get("price"))
            amount = safe_float(level.get("amount"))
            if price is None or amount is None or price <= 0 or amount <= 0:
                continue
            snapshot_bids[price] = amount

        for level in data.get("asks", []):
            price = safe_float(level.get("price"))
            amount = safe_float(level.get("amount"))
            if price is None or amount is None or price <= 0 or amount <= 0:
                continue
            snapshot_asks[price] = amount

        replace_full_book(EXCHANGE, snapshot_bids, snapshot_asks)
        return True

    if msg_type == "UPDATE":
        side_raw = data.get("side")
        if side_raw not in {"BUY", "SELL"}:
            return False

        side = "bids" if side_raw == "BUY" else "asks"
        price = safe_float(data.get("price"))
        amount = safe_float(data.get("amount"))
        if price is None or amount is None or price <= 0:
            return False

        update_level(EXCHANGE, side, price, amount)
        mark_book_update_applied()
        return True

    return False


async def stream() -> None:
    await run_exchange_stream(
        exchange=EXCHANGE,
        url=URL,
        handle_message=_handle_message,
        connect_kwargs=CONNECT_KWARGS,
    )
