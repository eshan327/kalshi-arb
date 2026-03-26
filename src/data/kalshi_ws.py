import json
import asyncio
import websockets
from core.config import WS_BASE_URL
from core.auth import get_ws_auth_headers


async def connect_and_subscribe(market_ticker: str):
    """
    Opens an authenticated WS connection and subscribes to orderbook + ticker channels.
    Returns the websocket connection for the caller to read from.
    """
    headers = get_ws_auth_headers()

    ws = await websockets.connect(WS_BASE_URL, additional_headers=headers)

    subscribe_cmd = {
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta", "ticker"],
            "market_tickers": [market_ticker]
        }
    }
    await ws.send(json.dumps(subscribe_cmd))
    return ws
