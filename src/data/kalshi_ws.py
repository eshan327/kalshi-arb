import json
from itertools import count

import websockets
from core.config import WS_BASE_URL
from core.auth import get_ws_auth_headers

SUBSCRIBE_CHANNELS = ["orderbook_delta"]
_command_ids = count(1)


async def connect_and_subscribe(market_ticker: str):
    """
    Opens an authenticated WS connection and subscribes to orderbook updates.
    Returns the websocket connection for the caller to read from.
    """
    headers = get_ws_auth_headers()

    ws = await websockets.connect(WS_BASE_URL, additional_headers=headers)

    subscribe_cmd = {
        "id": next(_command_ids),
        "cmd": "subscribe",
        "params": {
            "channels": SUBSCRIBE_CHANNELS,
            "market_tickers": [market_ticker],
            "use_yes_price": True,
        },
    }
    await ws.send(json.dumps(subscribe_cmd))
    return ws


async def request_orderbook_snapshot(ws, market_ticker: str, sid: int) -> None:
    """Request a sequence-aligned snapshot without replacing the subscription."""
    await ws.send(
        json.dumps(
            {
                "id": next(_command_ids),
                "cmd": "update_subscription",
                "params": {
                    "sids": [sid],
                    "market_tickers": [market_ticker],
                    "action": "get_snapshot",
                },
            }
        )
    )
