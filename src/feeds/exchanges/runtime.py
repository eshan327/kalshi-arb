import asyncio
import json
import logging
import random
from collections.abc import Callable
import websockets
from feeds.brti_state import init_exchange_book, record_exchange_ws_message

logger = logging.getLogger(__name__)

BRTI_RECONNECT_BASE_SEC = 1.0
BRTI_RECONNECT_MAX_SEC = 30.0


def _next_backoff(current_backoff: float) -> float:
    return min(current_backoff * 2.0, BRTI_RECONNECT_MAX_SEC)


async def _sleep_reconnect_backoff(exchange: str, err: Exception, current_backoff: float) -> float:
    jitter = random.uniform(0.0, current_backoff * 0.25)
    wait_for = current_backoff + jitter
    logger.warning("%s dropped (%s), reconnecting in %.2fs", exchange, err, wait_for)
    await asyncio.sleep(wait_for)
    return _next_backoff(current_backoff)


async def run_exchange_stream(
    *,
    exchange: str,
    url: str,
    handle_message: Callable[[dict], bool],
    subscribe_message: dict | None = None,
    connect_kwargs: dict | None = None,
) -> None:
    """Runs a reconnecting websocket loop for one exchange feed."""
    backoff = BRTI_RECONNECT_BASE_SEC
    kwargs = connect_kwargs or {}

    while True:
        try:
            init_exchange_book(exchange)
            async with websockets.connect(url, **kwargs) as ws:
                backoff = BRTI_RECONNECT_BASE_SEC

                if subscribe_message is not None:
                    await ws.send(json.dumps(subscribe_message))

                logger.info("%s L2 connected", exchange)

                async for message in ws:
                    data = json.loads(message)
                    record_exchange_ws_message(exchange, data, "received")

                    if handle_message(data):
                        record_exchange_ws_message(exchange, data, "parsed")

        except (websockets.ConnectionClosed, ConnectionError, OSError, json.JSONDecodeError) as exc:
            backoff = await _sleep_reconnect_backoff(exchange, exc, backoff)
