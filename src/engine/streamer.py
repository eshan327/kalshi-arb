import asyncio
import json
import websockets
from data.kalshi_rest import get_open_markets
from data.kalshi_ws import connect_and_subscribe
from engine.orderbook import OrderBook

# Live orderbook instance (accessible by other modules)
live_book = None


async def _stream_with_sync(market_ticker):
    """
    Connects WS, receives snapshot, then applies sequential deltas.
    On seq gap or disconnect: reconnect for a fresh snapshot.

    WS message flow:
    1. orderbook_snapshot (seq=1) — full book
    2. orderbook_delta (seq=2, 3, ...) — incremental changes
    """

    while True:
        try:
            live_book.reset()

            ws = await connect_and_subscribe(market_ticker)
            print(f"  --> Subscribed to {market_ticker}. Awaiting snapshot...")

            async for message in ws:
                data = json.loads(message)
                msg_type = data.get("type")
                seq = data.get("seq")

                if msg_type == "orderbook_snapshot":
                    live_book.check_seq(seq)
                    live_book.load_ws_snapshot(data.get("msg", {}))

                elif msg_type == "orderbook_delta":
                    if not live_book.initialized:
                        continue

                    if not live_book.check_seq(seq):
                        break  # seq gap — reconnect

                    live_book.apply_delta(data.get("msg", {}))

                elif msg_type == "ticker":
                    msg = data.get("msg", {})
                    print(f"  [TICK] yes_bid: {msg.get('yes_bid_dollars')} | yes_ask: {msg.get('yes_ask_dollars')}")

                elif msg_type == "subscribed":
                    print(f"  [SERVER] Subscription confirmed: {data.get('msg', {}).get('channel')}")

                # Check if resync needed after processing
                if live_book.needs_resync:
                    print("  --> Resync triggered, reconnecting...")
                    break

            await ws.close()

        except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
            print(f"  --> WebSocket dropped ({e}), reconnecting in 5s")

        await asyncio.sleep(5)


async def run_market_streamer():
    """Finds an active 15-minute crypto market and starts the sync loop."""
    global live_book

    print("Fetching active KXBTC15M market to stream.")
    markets = get_open_markets("KXBTC15M")

    if not markets:
        print("No active markets found.")
        return

    target_market = markets[0]['ticker']
    print(f"  --> Target market: {target_market}")

    live_book = OrderBook(target_market)
    await _stream_with_sync(target_market)
