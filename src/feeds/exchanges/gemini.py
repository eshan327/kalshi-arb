from feeds.brti_state import mark_book_update_applied, safe_float, update_level
from feeds.exchanges.runtime import run_exchange_stream

EXCHANGE = "GEMINI"
URL = "wss://api.gemini.com/v1/marketdata/BTCUSD"
CONNECT_KWARGS = {
    "max_size": 10_000_000,
    "open_timeout": 30,
    "ping_interval": 20,
    "ping_timeout": 10,
}


def _handle_message(data: dict) -> bool:
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
        price = safe_float(event.get("price"))
        remaining = safe_float(event.get("remaining"))
        if price is None or remaining is None or price <= 0:
            continue

        update_level(EXCHANGE, side, price, remaining)
        mark_book_update_applied()
        parsed = True

    return parsed


async def stream() -> None:
    await run_exchange_stream(
        exchange=EXCHANGE,
        url=URL,
        handle_message=_handle_message,
        connect_kwargs=CONNECT_KWARGS,
    )
