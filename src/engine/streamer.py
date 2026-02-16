import asyncio
from data.kalshi_rest import get_open_markets
from data.kalshi_ws import stream_market_data

def handle_ws_message(payload: dict):
    """Callback function acting as the router for incoming stream data."""
    
    msg_type = payload.get("type")
    
    if msg_type == "subscribed":
        print(f"  [SERVER] Subscription confirmed for channel: {payload.get('msg', {}).get('channel')}")
    elif msg_type == "orderbook_delta":
        # TODO: eventually route to math/arbitrage engine
        print(f"  [TICK] Orderbook updated -> {payload.get('msg')}")
    elif msg_type == "ticker":
        print(f"  [TICK] Price updated -> {payload.get('msg')}")

async def run_market_streamer():
    """Finds an active 15-minute crypto market and streams it."""

    print("Fetching active KXBTC15M market to stream.")
    markets = get_open_markets("KXBTC15M")
    
    if not markets:
        print("No active markets found.")
        return
        
    # Picks the first active market for the event loop
    target_market = markets[0]['ticker']
    await stream_market_data(target_market, handle_ws_message)