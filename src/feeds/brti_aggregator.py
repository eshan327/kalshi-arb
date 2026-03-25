import json
import time
import asyncio
import websockets
from feeds.brti_calc import calculate_brti

# Per-exchange L2 orderbook state
# {exchange: {"bids": {price: size}, "asks": {price: size}, "last_update": float}}
exchange_books = {}

# Latest BRTI output
current_brti = None
current_depth = 0
current_exchanges = 0


def _init_book(exchange):
    """Initialize an empty orderbook for an exchange."""
    exchange_books[exchange] = {
        "bids": {},
        "asks": {},
        "last_update": 0
    }


def _update_level(exchange, side, price, size):
    """Update a single price level in an exchange's orderbook."""
    book = exchange_books[exchange][side]
    if size <= 0:
        book.pop(price, None)
    else:
        book[price] = size
    exchange_books[exchange]["last_update"] = time.time()


# ---- Exchange Websocket Connections ----

async def _stream_coinbase():
    """Coinbase Advanced Trade L2 orderbook via public websocket."""
    exchange = "COINBASE"
    url = "wss://advanced-trade-ws.coinbase.com"

    while True:
        try:
            _init_book(exchange)
            async with websockets.connect(url, max_size=50_000_000, compression=None, ping_interval=20, ping_timeout=10) as ws:
                subscribe = {
                    "type": "subscribe",
                    "product_ids": ["BTC-USD"],
                    "channel": "level2"
                }
                await ws.send(json.dumps(subscribe))
                print(f"  --> {exchange} L2 connected")

                async for message in ws:
                    data = json.loads(message)
                    if data.get("channel") != "l2_data":
                        continue

                    for event in data.get("events", []):
                        event_type = event.get("type")

                        if event_type == "snapshot":
                            exchange_books[exchange]["bids"].clear()
                            exchange_books[exchange]["asks"].clear()

                        for update in event.get("updates", []):
                            side = "bids" if update["side"] == "bid" else "asks"
                            price = float(update["price_level"])
                            qty = float(update["new_quantity"])
                            _update_level(exchange, side, price, qty)

        except (websockets.ConnectionClosed, ConnectionError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(f"  --> {exchange} dropped ({e}), reconnecting in 5s")
            await asyncio.sleep(5)


async def _stream_kraken():
    """Kraken L2 orderbook via websocket v2."""
    exchange = "KRAKEN"
    url = "wss://ws.kraken.com/v2"

    while True:
        try:
            _init_book(exchange)
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                subscribe = {
                    "method": "subscribe",
                    "params": {
                        "channel": "book",
                        "symbol": ["BTC/USD"],
                        "depth": 1000,
                        "snapshot": True
                    }
                }
                await ws.send(json.dumps(subscribe))
                print(f"  --> {exchange} L2 connected")

                async for message in ws:
                    data = json.loads(message)
                    if data.get("channel") != "book":
                        continue

                    msg_type = data.get("type")
                    for entry in data.get("data", []):
                        if msg_type == "snapshot":
                            exchange_books[exchange]["bids"].clear()
                            exchange_books[exchange]["asks"].clear()

                        for level in entry.get("bids", []):
                            price, qty = float(level["price"]), float(level["qty"])
                            _update_level(exchange, "bids", price, qty)

                        for level in entry.get("asks", []):
                            price, qty = float(level["price"]), float(level["qty"])
                            _update_level(exchange, "asks", price, qty)

        except (websockets.ConnectionClosed, ConnectionError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(f"  --> {exchange} dropped ({e}), reconnecting in 5s")
            await asyncio.sleep(5)


async def _stream_gemini():
    """Gemini L2 orderbook via v1 marketdata websocket."""
    exchange = "GEMINI"
    url = "wss://api.gemini.com/v1/marketdata/BTCUSD"

    while True:
        try:
            _init_book(exchange)
            async with websockets.connect(url, max_size=10_000_000, ping_interval=20, ping_timeout=10) as ws:
                print(f"  --> {exchange} L2 connected")

                async for message in ws:
                    data = json.loads(message)
                    if data.get("type") != "update":
                        continue

                    for event in data.get("events", []):
                        if event.get("type") != "change":
                            continue

                        side = "bids" if event["side"] == "bid" else "asks"
                        price = float(event["price"])
                        remaining = float(event["remaining"])
                        _update_level(exchange, side, price, remaining)

        except (websockets.ConnectionClosed, ConnectionError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(f"  --> {exchange} dropped ({e}), reconnecting in 5s")
            await asyncio.sleep(5)


async def _stream_bitstamp():
    """Bitstamp L2 orderbook via websocket."""
    exchange = "BITSTAMP"
    url = "wss://ws.bitstamp.net"

    while True:
        try:
            _init_book(exchange)
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                subscribe = {
                    "event": "bts:subscribe",
                    "data": {
                        "channel": "order_book_btcusd"
                    }
                }
                await ws.send(json.dumps(subscribe))
                print(f"  --> {exchange} L2 connected")

                async for message in ws:
                    data = json.loads(message)
                    if data.get("event") != "data":
                        continue

                    orderbook = data.get("data", {})

                    # Bitstamp order_book channel sends full snapshots each time
                    exchange_books[exchange]["bids"].clear()
                    exchange_books[exchange]["asks"].clear()

                    for price_str, size_str in orderbook.get("bids", []):
                        price, size = float(price_str), float(size_str)
                        if size > 0:
                            exchange_books[exchange]["bids"][price] = size

                    for price_str, size_str in orderbook.get("asks", []):
                        price, size = float(price_str), float(size_str)
                        if size > 0:
                            exchange_books[exchange]["asks"][price] = size

                    exchange_books[exchange]["last_update"] = time.time()

        except (websockets.ConnectionClosed, ConnectionError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(f"  --> {exchange} dropped ({e}), reconnecting in 5s")
            await asyncio.sleep(5)


# ---- BRTI Recalculation Loop ----

async def _recalculate_loop():
    """Recalculates the BRTI every second from the live consolidated orderbook."""
    global current_brti, current_depth, current_exchanges

    # Wait for orderbooks to populate
    await asyncio.sleep(3)
    print("  --> BRTI recalculation loop started (1s interval)")

    while True:
        now = time.time()
        brti, depth, num_exchanges = calculate_brti(exchange_books, now)

        if brti is not None:
            current_brti = brti
            current_depth = depth
            current_exchanges = num_exchanges

            # Debug output
            book_sizes = {name: len(b["bids"]) + len(b["asks"]) for name, b in exchange_books.items()}
            print(f"  [BRTI] ${brti:,.2f} | depth: {depth} BTC | exchanges: {num_exchanges} | levels: {book_sizes}")
        else:
            print(f"  [BRTI] Calculation failed | exchanges with data: {len(exchange_books)}")

        await asyncio.sleep(1)


def run():
    """Starts all four exchange L2 feeds and the BRTI recalculation loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.create_task(_stream_coinbase())
    loop.create_task(_stream_kraken())
    loop.create_task(_stream_gemini())
    loop.create_task(_stream_bitstamp())
    loop.create_task(_recalculate_loop())

    print("  --> BRTI aggregator started (Coinbase, Kraken, Gemini, Bitstamp)")
    loop.run_forever()
