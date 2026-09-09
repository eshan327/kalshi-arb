import json
from itertools import count

import websockets

from core.auth import get_ws_auth_headers
from core.config import WS_BASE_URL

_command_ids = count(1)


async def command(ws, cmd: str, **params) -> int:
    command_id = next(_command_ids)
    await ws.send(json.dumps({"id": command_id, "cmd": cmd, "params": params}))
    return command_id


async def connect():
    return await websockets.connect(
        WS_BASE_URL,
        additional_headers=get_ws_auth_headers(),
        ping_interval=10,
        ping_timeout=10,
        open_timeout=10,
    )


async def request_orderbook_snapshot(ws, market_ticker: str, sid: int) -> None:
    await command(
        ws,
        "update_subscription",
        sids=[sid],
        market_tickers=[market_ticker],
        action="get_snapshot",
    )
