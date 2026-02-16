import json
import asyncio
import websockets
from core.config import WS_BASE_URL
from core.auth import get_ws_auth_headers

async def stream_market_data(market_ticker: str, callback_func):
    """
    Streams updated from Websocket connection to a callback function.
    """
    
    headers = get_ws_auth_headers()
    
    print(f"  --> Connecting to WebSocket: {WS_BASE_URL}")
    
    async with websockets.connect(WS_BASE_URL, additional_headers=headers) as ws:
        # Subscribe to orderbook and ticker updates
        subscribe_cmd = {
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta", "ticker"],
                "market_tickers": [market_ticker]
            }
        }
        await ws.send(json.dumps(subscribe_cmd))
        print(f"  --> Subscribed to {market_ticker} stream. Awaiting data...\n")
        
        # Infinite event loop to catch push data
        async for message in ws:
            data = json.loads(message)
            callback_func(data)