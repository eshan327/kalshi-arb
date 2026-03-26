from feeds.brti_state import mark_book_update_applied, replace_full_book, safe_float, update_level
from feeds.exchanges.runtime import run_exchange_stream

EXCHANGE = "COINBASE"
URL = "wss://advanced-trade-ws.coinbase.com"
SUBSCRIBE = {
    "type": "subscribe",
    "product_ids": ["BTC-USD"],
    "channel": "level2",
}
CONNECT_KWARGS = {
    "max_size": 50_000_000,
    "compression": None,
    "ping_interval": 20,
    "ping_timeout": 10,
}


def _handle_message(data: dict) -> bool:
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


async def stream() -> None:
    await run_exchange_stream(
        exchange=EXCHANGE,
        url=URL,
        handle_message=_handle_message,
        subscribe_message=SUBSCRIBE,
        connect_kwargs=CONNECT_KWARGS,
    )
