import json
import time
import asyncio
from collections import deque
from threading import Lock
import websockets
from feeds.brti_calc import calculate_brti

# Per-exchange L2 orderbook state
# {exchange: {"bids": {price: size}, "asks": {price: size}, "last_update": float}}
exchange_books = {}

# Latest BRTI output
current_brti = None
current_depth = 0
current_exchanges = 0
current_brti_ts = 0.0
_brti_ticks = deque(maxlen=2000)
_brti_ticks_lock = Lock()
_exchange_ws_log = deque(maxlen=5000)
_exchange_ws_stats = {
    "total_received": 0,
    "total_parsed": 0,
    "coinbase_received": 0,
    "coinbase_parsed": 0,
    "kraken_received": 0,
    "kraken_parsed": 0,
    "gemini_received": 0,
    "gemini_parsed": 0,
    "bitstamp_received": 0,
    "bitstamp_parsed": 0,
    "paxos_received": 0,
    "paxos_parsed": 0,
    "book_updates_applied": 0,
}


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _record_brti_tick(brti, depth, num_exchanges, levels, status):
    with _brti_ticks_lock:
        _brti_ticks.append(
            {
                "ts": time.time(),
                "brti": brti,
                "depth": depth,
                "exchanges": num_exchanges,
                "levels": levels,
                "status": status,
            }
        )


def _record_exchange_ws_message(exchange, raw_data, status):
    with _brti_ticks_lock:
        suffix = "received" if status == "received" else "parsed"
        total_key = "total_received" if suffix == "received" else "total_parsed"
        _exchange_ws_stats[total_key] += 1

        key = f"{exchange.lower()}_{suffix}"
        if key in _exchange_ws_stats:
            _exchange_ws_stats[key] += 1
        _exchange_ws_log.append(
            {
                "ts": time.time(),
                "exchange": exchange,
                "status": status,
                "raw_type": raw_data.get("type") if isinstance(raw_data, dict) else None,
                "raw_channel": raw_data.get("channel") if isinstance(raw_data, dict) else None,
                "raw_event": raw_data.get("event") if isinstance(raw_data, dict) else None,
            }
        )


def _mark_book_update_applied():
    with _brti_ticks_lock:
        _exchange_ws_stats["book_updates_applied"] += 1


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
                    _record_exchange_ws_message(exchange, data, "received")
                    if data.get("channel") != "l2_data":
                        continue

                    parsed = False

                    for event in data.get("events", []):
                        event_type = event.get("type")

                        if event_type == "snapshot":
                            exchange_books[exchange]["bids"].clear()
                            exchange_books[exchange]["asks"].clear()

                        for update in event.get("updates", []):
                            side_raw = update.get("side")
                            if side_raw not in {"bid", "ask", "offer"}:
                                continue

                            side = "bids" if side_raw == "bid" else "asks"
                            price = _safe_float(update.get("price_level"))
                            qty = _safe_float(update.get("new_quantity"))
                            if price is None or qty is None or price <= 0:
                                continue

                            _update_level(exchange, side, price, qty)
                            _mark_book_update_applied()
                            parsed = True

                    if parsed:
                        _record_exchange_ws_message(exchange, data, "parsed")

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
                    _record_exchange_ws_message(exchange, data, "received")
                    if data.get("channel") != "book":
                        continue

                    msg_type = data.get("type")
                    parsed = False
                    for entry in data.get("data", []):
                        if msg_type == "snapshot":
                            exchange_books[exchange]["bids"].clear()
                            exchange_books[exchange]["asks"].clear()

                        for level in entry.get("bids", []):
                            price = _safe_float(level.get("price"))
                            qty = _safe_float(level.get("qty"))
                            if price is None or qty is None or price <= 0:
                                continue
                            _update_level(exchange, "bids", price, qty)
                            _mark_book_update_applied()
                            parsed = True

                        for level in entry.get("asks", []):
                            price = _safe_float(level.get("price"))
                            qty = _safe_float(level.get("qty"))
                            if price is None or qty is None or price <= 0:
                                continue
                            _update_level(exchange, "asks", price, qty)
                            _mark_book_update_applied()
                            parsed = True

                    if parsed:
                        _record_exchange_ws_message(exchange, data, "parsed")

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
            async with websockets.connect(url, max_size=10_000_000, open_timeout=30, ping_interval=20, ping_timeout=10) as ws:
                print(f"  --> {exchange} L2 connected")

                async for message in ws:
                    data = json.loads(message)
                    _record_exchange_ws_message(exchange, data, "received")
                    if data.get("type") != "update":
                        continue

                    parsed = False

                    for event in data.get("events", []):
                        if event.get("type") != "change":
                            continue

                        side_raw = event.get("side")
                        if side_raw not in {"bid", "ask"}:
                            continue

                        side = "bids" if side_raw == "bid" else "asks"
                        price = _safe_float(event.get("price"))
                        remaining = _safe_float(event.get("remaining"))
                        if price is None or remaining is None or price <= 0:
                            continue

                        _update_level(exchange, side, price, remaining)
                        _mark_book_update_applied()
                        parsed = True

                    if parsed:
                        _record_exchange_ws_message(exchange, data, "parsed")

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
                    _record_exchange_ws_message(exchange, data, "received")
                    if data.get("event") != "data":
                        continue

                    orderbook = data.get("data", {})

                    # Bitstamp order_book channel sends full snapshots each time
                    exchange_books[exchange]["bids"].clear()
                    exchange_books[exchange]["asks"].clear()

                    for price_str, size_str in orderbook.get("bids", []):
                        price = _safe_float(price_str)
                        size = _safe_float(size_str)
                        if price is None or size is None or price <= 0:
                            continue
                        if size > 0:
                            exchange_books[exchange]["bids"][price] = size

                    for price_str, size_str in orderbook.get("asks", []):
                        price = _safe_float(price_str)
                        size = _safe_float(size_str)
                        if price is None or size is None or price <= 0:
                            continue
                        if size > 0:
                            exchange_books[exchange]["asks"][price] = size

                    exchange_books[exchange]["last_update"] = time.time()
                    _mark_book_update_applied()
                    _record_exchange_ws_message(exchange, data, "parsed")

        except (websockets.ConnectionClosed, ConnectionError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(f"  --> {exchange} dropped ({e}), reconnecting in 5s")
            await asyncio.sleep(5)


async def _stream_paxos():
    """Paxos (itBit) L2 orderbook via public websocket."""
    exchange = "PAXOS"
    url = "wss://ws.paxos.com/marketdata/BTCUSD"

    while True:
        try:
            _init_book(exchange)
            async with websockets.connect(url, max_size=10_000_000, ping_interval=20, ping_timeout=10) as ws:
                print(f"  --> {exchange} L2 connected")

                async for message in ws:
                    data = json.loads(message)
                    _record_exchange_ws_message(exchange, data, "received")
                    msg_type = data.get("type")

                    if msg_type == "SNAPSHOT":
                        exchange_books[exchange]["bids"].clear()
                        exchange_books[exchange]["asks"].clear()
                        for level in data.get("bids", []):
                            price = _safe_float(level.get("price"))
                            amount = _safe_float(level.get("amount"))
                            if price is None or amount is None or price <= 0:
                                continue
                            if amount > 0:
                                exchange_books[exchange]["bids"][price] = amount
                        for level in data.get("asks", []):
                            price = _safe_float(level.get("price"))
                            amount = _safe_float(level.get("amount"))
                            if price is None or amount is None or price <= 0:
                                continue
                            if amount > 0:
                                exchange_books[exchange]["asks"][price] = amount
                        exchange_books[exchange]["last_update"] = time.time()
                        _mark_book_update_applied()
                        _record_exchange_ws_message(exchange, data, "parsed")

                    elif msg_type == "UPDATE":
                        side_raw = data.get("side")
                        if side_raw not in {"BUY", "SELL"}:
                            continue

                        side = "bids" if side_raw == "BUY" else "asks"
                        price = _safe_float(data.get("price"))
                        amount = _safe_float(data.get("amount"))
                        if price is None or amount is None or price <= 0:
                            continue

                        _update_level(exchange, side, price, amount)
                        _mark_book_update_applied()
                        _record_exchange_ws_message(exchange, data, "parsed")

        except (websockets.ConnectionClosed, ConnectionError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(f"  --> {exchange} dropped ({e}), reconnecting in 5s")
            await asyncio.sleep(5)


# ---- BRTI Recalculation Loop ----

async def _recalculate_loop(recalc_interval=1.0):
    """Recalculates the BRTI every second from the live consolidated orderbook."""
    global current_brti, current_depth, current_exchanges, current_brti_ts

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
            current_brti_ts = now

            book_sizes = {name: len(b["bids"]) + len(b["asks"]) for name, b in exchange_books.items()}
            _record_brti_tick(brti, depth, num_exchanges, book_sizes, "ok")
        else:
            _record_brti_tick(None, 0, 0, {}, "calc_failed")

        await asyncio.sleep(recalc_interval)


def get_brti_state():
    """Returns latest BRTI snapshot for downstream consumers."""
    return {
        "brti": current_brti,
        "depth": current_depth,
        "exchanges": current_exchanges,
        "timestamp": current_brti_ts,
    }


def get_brti_ticks(limit=200):
    """Returns newest synthesized BRTI ticks for dashboard verification."""
    with _brti_ticks_lock:
        if limit <= 0:
            return []
        return list(_brti_ticks)[-limit:]


def get_brti_settlement_proxy(window_seconds=60):
    """Returns rolling average of valid BRTI prints over the given lookback window."""
    now = time.time()
    cutoff = now - max(1, window_seconds)

    with _brti_ticks_lock:
        window_values = [
            float(tick["brti"])
            for tick in _brti_ticks
            if tick.get("status") == "ok"
            and isinstance(tick.get("brti"), (int, float))
            and tick.get("ts", 0) >= cutoff
        ]

    if not window_values:
        return {
            "window_seconds": window_seconds,
            "samples": 0,
            "average": None,
        }

    return {
        "window_seconds": window_seconds,
        "samples": len(window_values),
        "average": round(sum(window_values) / len(window_values), 2),
    }


def get_brti_ws_log(limit=200):
    """Returns newest raw exchange websocket events feeding BRTI."""
    with _brti_ticks_lock:
        if limit <= 0:
            return []
        return list(_exchange_ws_log)[-limit:]


def get_brti_ws_stats():
    """Returns aggregate counters proving BRTI websocket message processing."""
    with _brti_ticks_lock:
        return dict(_exchange_ws_stats)


async def run_brti_aggregator(recalc_interval=1.0):
    """Starts exchange L2 feeds and the BRTI recalculation loop in the current event loop."""
    tasks = [
        asyncio.create_task(_stream_coinbase()),
        asyncio.create_task(_stream_kraken()),
        asyncio.create_task(_stream_gemini()),
        asyncio.create_task(_stream_bitstamp()),
        asyncio.create_task(_stream_paxos()),
        asyncio.create_task(_recalculate_loop(recalc_interval=recalc_interval)),
    ]

    print("  --> BRTI aggregator started (Coinbase, Kraken, Gemini, Bitstamp, Paxos)")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
