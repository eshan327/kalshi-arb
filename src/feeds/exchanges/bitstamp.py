from feeds.brti_state import replace_full_book, safe_float
from feeds.exchanges.runtime import run_exchange_stream

EXCHANGE = "BITSTAMP"
URL = "wss://ws.bitstamp.net"
SUBSCRIBE = {
    "event": "bts:subscribe",
    "data": {"channel": "order_book_btcusd"},
}
CONNECT_KWARGS = {
    "ping_interval": 20,
    "ping_timeout": 10,
}


def _handle_message(data: dict) -> bool:
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


async def stream() -> None:
    await run_exchange_stream(
        exchange=EXCHANGE,
        url=URL,
        handle_message=_handle_message,
        subscribe_message=SUBSCRIBE,
        connect_kwargs=CONNECT_KWARGS,
    )
